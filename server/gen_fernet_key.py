#!/usr/bin/env python3
"""Generate a Fernet key for IDME_ENCRYPTION_KEY.

A Fernet key is 32 random bytes, base64-encoded (a 44-char string ending in '=').
The IDME module uses it to encrypt teacher portal credentials at rest in the DB.

Generate it ONCE and keep it safe:
  - If you change/lose it, already-stored teacher credentials can't be decrypted.
  - Treat it like a password; never commit it to git.

Run anywhere with `cryptography` installed. On CT 105 the system python3 lacks
it and the smartt image lacks this script, so from ~/Datang-Reader:
    docker exec -i smartt python - < server/gen_fernet_key.py

Then copy the printed line into your .env as:
    IDME_ENCRYPTION_KEY=<the key>
"""

from cryptography.fernet import Fernet


def main() -> None:
    key = Fernet.generate_key().decode()
    print(key)


if __name__ == "__main__":
    main()
