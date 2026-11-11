"""
Teacher Manager for IDME Module

CRUD operations for teacher credentials stored in idme_data.db.
Teachers are managed via the Web UI at /idme/settings.
Passwords are Fernet-encrypted before storage.
"""

import sqlite3
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

from .credential_manager import CredentialManager, DecryptionError


class TeacherManagerError(Exception):
    """Base exception for teacher manager errors."""
    pass


class TeacherManager:
    """
    Manages teacher credentials in the IDME database.

    Teachers are added/edited/deleted via the Web UI.
    Passwords are encrypted with Fernet before storage.
    """

    def __init__(self, db_path: str, credential_manager: CredentialManager):
        """
        Initialize teacher manager.

        Args:
            db_path: Path to idme_data.db SQLite database.
            credential_manager: CredentialManager for password encryption.
        """
        self.db_path = Path(db_path)
        self.credential_manager = credential_manager
        self.logger = logging.getLogger(__name__)

        # Ensure database and table exist
        self._ensure_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection with WAL mode for concurrent access."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_db(self):
        """Ensure the database and teachers table exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        try:
            # Read and execute schema
            schema_path = Path(__file__).parent / 'schema.sql'
            if schema_path.exists():
                conn.executescript(schema_path.read_text())
            else:
                # Fallback: create just the teachers table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS teachers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        ic_number TEXT NOT NULL UNIQUE,
                        encrypted_password TEXT NOT NULL,
                        class_name TEXT NOT NULL,
                        enabled BOOLEAN DEFAULT 1,
                        login_test_status TEXT,
                        login_test_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            # Add any additive columns missing from an already-created teachers
            # table (e.g. login_test_* on a pre-existing prod DB). Idempotent.
            from .migrations import apply_migrations
            apply_migrations(conn)
            conn.commit()
        finally:
            conn.close()

    def add_teacher(
        self,
        name: str,
        ic_number: str,
        password: str,
        class_name: str
    ) -> Dict[str, Any]:
        """
        Add a new teacher. Password is encrypted before storage.

        Args:
            name: Teacher full name.
            ic_number: Malaysian IC number (12 digits, used for IDME login).
            password: Plaintext password (will be encrypted).
            class_name: Class assigned to this teacher (e.g., '5 UKM').

        Returns:
            Dictionary with the created teacher info (no password).

        Raises:
            TeacherManagerError: If IC number already exists or other DB error.
        """
        # Validate IC number format
        ic_clean = ic_number.strip().replace('-', '')
        if not ic_clean.isdigit() or len(ic_clean) != 12:
            raise TeacherManagerError(
                f"Invalid IC number: must be exactly 12 digits, got {len(ic_clean)} characters"
            )

        # Encrypt password
        encrypted = self.credential_manager.encrypt_password(password)

        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """INSERT INTO teachers (name, ic_number, encrypted_password, class_name)
                   VALUES (?, ?, ?, ?)""",
                (name.strip().upper(), ic_clean, encrypted, class_name.strip())
            )
            conn.commit()

            teacher_id = cursor.lastrowid
            self.logger.info(f"Added teacher: {name} (ID={teacher_id}, class={class_name})")

            return self.get_teacher(teacher_id)
        except sqlite3.IntegrityError:
            raise TeacherManagerError(
                f"Teacher with IC {ic_clean} already exists."
            )
        except sqlite3.Error as e:
            raise TeacherManagerError(f"Failed to add teacher: {e}")
        finally:
            conn.close()

    def update_teacher(
        self,
        teacher_id: int,
        name: Optional[str] = None,
        ic_number: Optional[str] = None,
        password: Optional[str] = None,
        class_name: Optional[str] = None,
        enabled: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Update teacher fields. Only provided fields are updated.
        Password is re-encrypted if changed.

        Args:
            teacher_id: Teacher database ID.
            name: New name (optional).
            ic_number: New IC number (optional).
            password: New plaintext password (optional, will be encrypted).
            class_name: New class assignment (optional).
            enabled: Enable/disable teacher (optional).

        Returns:
            Updated teacher dictionary.
        """
        updates = []
        params = []

        if name is not None:
            updates.append("name = ?")
            params.append(name.strip().upper())

        if ic_number is not None:
            ic_clean = ic_number.strip().replace('-', '')
            if not ic_clean.isdigit() or len(ic_clean) != 12:
                raise TeacherManagerError(
                    f"Invalid IC number: must be exactly 12 digits, got {len(ic_clean)} characters")
            updates.append("ic_number = ?")
            params.append(ic_clean)

        if password is not None:
            encrypted = self.credential_manager.encrypt_password(password)
            updates.append("encrypted_password = ?")
            params.append(encrypted)

        if class_name is not None:
            updates.append("class_name = ?")
            params.append(class_name.strip())

        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)

        if not updates:
            return self.get_teacher(teacher_id)

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(teacher_id)

        conn = self._get_conn()
        try:
            conn.execute(
                f"UPDATE teachers SET {', '.join(updates)} WHERE id = ?",
                params
            )
            conn.commit()
            self.logger.info(f"Updated teacher ID={teacher_id}")
            return self.get_teacher(teacher_id)
        except sqlite3.IntegrityError:
            raise TeacherManagerError("IC number already used by another teacher.")
        except sqlite3.Error as e:
            raise TeacherManagerError(f"Failed to update teacher: {e}")
        finally:
            conn.close()

    def delete_teacher(self, teacher_id: int) -> bool:
        """
        Delete a teacher and their session cache.

        Args:
            teacher_id: Teacher database ID.

        Returns:
            True if deleted successfully.
        """
        conn = self._get_conn()
        try:
            # Delete session cache
            conn.execute("DELETE FROM session_cache WHERE teacher_id = ?", (teacher_id,))
            # Delete teacher
            cursor = conn.execute("DELETE FROM teachers WHERE id = ?", (teacher_id,))
            conn.commit()

            if cursor.rowcount > 0:
                self.logger.info(f"Deleted teacher ID={teacher_id}")
                return True
            return False
        except sqlite3.Error as e:
            raise TeacherManagerError(f"Failed to delete teacher: {e}")
        finally:
            conn.close()

    def get_teacher(self, teacher_id: int) -> Optional[Dict[str, Any]]:
        """
        Get teacher by ID (no password exposed).

        Args:
            teacher_id: Teacher database ID.

        Returns:
            Teacher dict or None if not found.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, name, ic_number, class_name, enabled, "
                "login_test_status, login_test_at, telegram_chat_id, "
                "created_at, updated_at "
                "FROM teachers WHERE id = ?",
                (teacher_id,)
            ).fetchone()

            if not row:
                return None

            return dict(row)
        finally:
            conn.close()

    def get_all_teachers(self, include_disabled: bool = False) -> List[Dict[str, Any]]:
        """
        Get all teachers (no passwords exposed).

        Args:
            include_disabled: If True, include disabled teachers.

        Returns:
            List of teacher dictionaries.
        """
        conn = self._get_conn()
        try:
            cols = ("id, name, ic_number, class_name, enabled, "
                    "login_test_status, login_test_at, telegram_chat_id, "
                    "created_at, updated_at")
            if include_disabled:
                rows = conn.execute(
                    f"SELECT {cols} FROM teachers ORDER BY name"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {cols} FROM teachers WHERE enabled = 1 ORDER BY name"
                ).fetchall()

            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_teacher_for_class(self, class_name: str) -> Optional[Dict[str, Any]]:
        """
        Find the teacher assigned to a class.

        Args:
            class_name: Class name (e.g., '5 UKM').

        Returns:
            Teacher dict or None.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, name, ic_number, class_name, enabled, telegram_chat_id, "
                "created_at, updated_at "
                "FROM teachers WHERE class_name = ? AND enabled = 1",
                (class_name,)
            ).fetchone()

            return dict(row) if row else None
        finally:
            conn.close()

    def get_teacher_credentials(self, teacher_id: int) -> Dict[str, str]:
        """
        Get teacher credentials with DECRYPTED password.
        Used internally by the automation engine.

        Args:
            teacher_id: Teacher database ID.

        Returns:
            Dict with 'name', 'ic_number', 'password' (decrypted), 'class_name'.

        Raises:
            TeacherManagerError: If teacher not found.
            DecryptionError: If password decryption fails.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT name, ic_number, encrypted_password, class_name "
                "FROM teachers WHERE id = ? AND enabled = 1",
                (teacher_id,)
            ).fetchone()

            if not row:
                raise TeacherManagerError(f"Teacher ID={teacher_id} not found or disabled.")

            # Decrypt password
            plaintext = self.credential_manager.decrypt_password(row['encrypted_password'])

            return {
                'name': row['name'],
                'ic_number': row['ic_number'],
                'password': plaintext,
                'class_name': row['class_name'],
            }
        finally:
            conn.close()

    def record_login_test(self, teacher_id: int, success: bool) -> None:
        """Persist the result of a login probe so the settings UI can show a
        Verified / Wrong-password chip that survives a reload and can expire.

        Args:
            teacher_id: Teacher database ID.
            success: True if the IDME login succeeded.
        """
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE teachers SET login_test_status = ?, login_test_at = ? WHERE id = ?",
                ('ok' if success else 'fail', datetime.now().isoformat(), teacher_id)
            )
            conn.commit()
        except sqlite3.Error as e:
            # Non-fatal: the probe already ran; failing to cache it just means
            # the chip stays 'untested'. Never let it mask the test result.
            self.logger.warning(f"Failed to record login test for teacher {teacher_id}: {e}")
        finally:
            conn.close()

    def link_telegram_chat(self, teacher_id: int, chat_id: str) -> Optional[Dict[str, Any]]:
        """Bind a Telegram chat to a teacher.

        A chat maps to exactly one teacher, so any prior binding of this chat_id
        (to another teacher) is cleared first. Called by the bot once the teacher
        has passed the passphrase gate and tapped their class.

        Args:
            teacher_id: Teacher database ID (resolved from the chosen class).
            chat_id: The Telegram chat id to bind (stored as text).

        Returns:
            The linked teacher dict, or None if the teacher_id doesn't exist.
        """
        chat_id = str(chat_id)
        conn = self._get_conn()
        try:
            now = datetime.now().isoformat()
            # One chat ↔ one teacher: drop this chat from any other teacher first.
            conn.execute(
                "UPDATE teachers SET telegram_chat_id = NULL, updated_at = ? "
                "WHERE telegram_chat_id = ? AND id != ?",
                (now, chat_id, teacher_id)
            )
            cursor = conn.execute(
                "UPDATE teachers SET telegram_chat_id = ?, updated_at = ? WHERE id = ?",
                (chat_id, now, teacher_id)
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
        except sqlite3.Error as e:
            raise TeacherManagerError(f"Failed to link telegram chat: {e}")
        finally:
            conn.close()

        self.logger.info(f"Linked Telegram chat {chat_id} to teacher ID={teacher_id}")
        return self.get_teacher(teacher_id)

    def unlink_telegram_chat(self, teacher_id: int) -> None:
        """Clear a teacher's Telegram chat binding (admin action, frees the class
        back into the bot's selectable list so a new phone can link)."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE teachers SET telegram_chat_id = NULL, updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), teacher_id)
            )
            conn.commit()
        except sqlite3.Error as e:
            raise TeacherManagerError(f"Failed to unlink telegram chat: {e}")
        finally:
            conn.close()

    def get_unlinked_configured_classes(self) -> List[str]:
        """Classes with an enabled teacher that have no Telegram chat linked yet.

        This is exactly the set the bot offers a teacher to pick from: an already-
        linked class never appears, so a passphrase-holder can't re-point a
        colleague's class to their own chat (an admin must unlink it first).
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT class_name FROM teachers "
                "WHERE enabled = 1 AND telegram_chat_id IS NULL ORDER BY class_name"
            ).fetchall()
            return [row['class_name'] for row in rows]
        finally:
            conn.close()

    def get_teacher_by_chat_id(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Find the enabled teacher bound to a Telegram chat id, or None."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, name, ic_number, class_name, enabled, telegram_chat_id "
                "FROM teachers WHERE telegram_chat_id = ? AND enabled = 1",
                (str(chat_id),)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_configured_classes(self) -> List[str]:
        """
        Get list of all classes that have an enabled teacher assigned.

        Returns:
            List of class names.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT class_name FROM teachers WHERE enabled = 1 ORDER BY class_name"
            ).fetchall()
            return [row['class_name'] for row in rows]
        finally:
            conn.close()
