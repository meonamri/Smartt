#!/usr/bin/env python3
"""
Smartt Linux Service - Main Entry Point

This is the main script to run Smartt (Smart Attendance Record Tracking
Tajuddin), the school-owned attendance service, on Linux.
"""

import sys
import os
import argparse
import logging
from logging.handlers import RotatingFileHandler

# Add src directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Config
from src.service_manager import ServiceManager


class PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler whose files are owner-only (0600).

    The log records teacher logins and attendance activity. Rotation opens a
    fresh file with the process umask (world-readable by default), so set the
    mode every time a file is opened, not just once at startup.
    """

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def setup_logging(log_level: str = None, log_file: str = None):
    """
    Setup logging configuration

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file. If None, use config default.
    """
    log_level = log_level or Config.LOG_LEVEL
    log_file = log_file or Config.LOG_FILE

    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # File handler with rotation
    try:
        file_handler = PrivateRotatingFileHandler(
            log_file,
            maxBytes=Config.LOG_MAX_BYTES,
            backupCount=Config.LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        handlers = [console_handler, file_handler]
    except Exception as e:
        print(f"Warning: Could not create log file: {e}")
        handlers = [console_handler]

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        handlers=handlers
    )

    # Reduce noise from some libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PyQt5").setLevel(logging.WARNING)


def run_gui_mode(service_manager: ServiceManager):
    """
    Run service in GUI mode

    Args:
        service_manager: Service manager instance
    """
    logger = logging.getLogger(__name__)
    logger.info("Starting GUI mode...")

    try:
        # Import GUI module
        from src.gui_app import run_gui

        # Start service
        if not service_manager.start():
            logger.error("Failed to start service")
            print("Failed to start service. Check logs for details.")
            return 1

        # Run GUI
        run_gui(service_manager)
        return 0

    except ImportError as e:
        logger.error(f"Failed to import GUI module: {e}")
        print("Error: PyQt5 not installed. Install with: pip install PyQt5")
        return 1
    except Exception as e:
        logger.error(f"Error in GUI mode: {e}")
        return 1


def run_console_mode(service_manager: ServiceManager):
    """
    Run service in console mode

    Args:
        service_manager: Service manager instance
    """
    logger = logging.getLogger(__name__)
    logger.info("Starting console mode...")

    try:
        service_manager.run_console_mode()
        return 0
    except Exception as e:
        logger.error(f"Error in console mode: {e}")
        return 1


def run_http_server_mode(service_manager: ServiceManager, host: str = '0.0.0.0', port: int = 8080):
    """
    Run service in HTTP server mode (for Docker deployment)

    Args:
        service_manager: Service manager instance
        host: Host to bind to (default: 0.0.0.0)
        port: Port to listen on (default: 8080)
    """
    logger = logging.getLogger(__name__)
    logger.info("Starting HTTP server mode...")

    try:
        from src.http_server import run_http_server
        run_http_server(host=host, port=port, use_mock_api=service_manager.use_mock_api)
        return 0
    except ImportError as e:
        logger.error(f"Failed to import HTTP server module: {e}")
        print("Error: Flask not installed. Install with: pip install flask")
        return 1
    except Exception as e:
        logger.error(f"Error in HTTP server mode: {e}")
        return 1


def run_status_command():
    """Display service status"""
    print("\nSmartt Service Status")
    print("="*60)

    try:
        # Check configuration
        is_valid, errors = Config.validate()
        if is_valid:
            print("✓ Configuration: Valid")
        else:
            print("✗ Configuration: Invalid")
            for error in errors:
                print(f"  - {error}")

        # Show configuration
        print(f"\nAPI URL: {Config.API_BASE_URL}")
        print(f"Device ID: {Config.DEVICE_ID}")
        print(f"Serial Port: {Config.SERIAL_PORT or 'auto-detect'}")
        print(f"Baud Rate: {Config.SERIAL_BAUD_RATE}")

        # Check RFID reader
        from src.rfid_reader import RFIDReader
        print(f"\nAvailable serial ports:")
        ports = RFIDReader.list_available_ports()
        if ports:
            for port, description in ports:
                print(f"  - {port}: {description}")
        else:
            print("  (none found)")

        # Queue status
        from src.offline_queue import AttendanceQueue
        queue = AttendanceQueue()
        stats = queue.get_statistics()
        print(f"\nQueue Status:")
        print(f"  Pending: {stats.get('pending', 0)}")
        print(f"  Synced: {stats.get('synced', 0)}")
        print(f"  Failed: {stats.get('failed', 0)}")

        # Token status
        from src.auth_manager import AuthManager
        from src.api_client import DatangAPIClient
        api_client = DatangAPIClient()
        auth_manager = AuthManager(api_client)

        if auth_manager.load_token():
            print(f"\n✓ Authentication token: Loaded")
        else:
            print(f"\n✗ Authentication token: Not found")
            print(f"  Run with --login to authenticate")

        print()
        return 0

    except Exception as e:
        print(f"\nError: {e}")
        return 1


def run_login_command():
    """Perform login and save token"""
    print("\nSmartt - Login")
    print("="*60)

    try:
        from src.api_client import DatangAPIClient
        from src.auth_manager import AuthManager

        api_client = DatangAPIClient()
        auth_manager = AuthManager(api_client)

        # Check credentials
        is_valid, error = auth_manager.verify_credentials()
        if not is_valid:
            print(f"Error: {error}")
            print("\nPlease set credentials in configuration:")
            print("  READER_USERNAME: Set in src/config.py or environment variable")
            print("  READER_PASSWORD: Set in src/config.py or environment variable")
            return 1

        print(f"Attempting login with username: {Config.READER_USERNAME}")

        if auth_manager.login():
            print("✓ Login successful! Token saved.")
            return 0
        else:
            print("✗ Login failed. Check credentials and API configuration.")
            return 1

    except Exception as e:
        print(f"Error: {e}")
        return 1


def run_sync_command():
    """Manually sync offline queue"""
    print("\nSmartt - Queue Sync")
    print("="*60)

    try:
        from src.api_client import DatangAPIClient
        from src.auth_manager import AuthManager
        from src.offline_queue import AttendanceQueue

        # Initialize components
        api_client = DatangAPIClient()
        auth_manager = AuthManager(api_client)
        queue = AttendanceQueue()

        # Check queue
        queue_size = queue.get_queue_size()
        print(f"Pending records: {queue_size}")

        if queue_size == 0:
            print("Nothing to sync.")
            return 0

        # Authenticate
        print("Authenticating...")
        if not auth_manager.ensure_authenticated():
            print("✗ Authentication failed")
            return 1

        print("✓ Authenticated")

        # Sync
        print(f"\nSyncing {queue_size} records...")
        stats = queue.sync_with_api(api_client)

        print(f"\nSync Results:")
        print(f"  Total: {stats['total']}")
        print(f"  ✓ Synced: {stats['synced']}")
        print(f"  ✗ Failed: {stats['failed']}")
        print(f"  ⊘ Skipped: {stats['skipped']}")

        return 0

    except Exception as e:
        print(f"Error: {e}")
        return 1


def run_test_reader_command():
    """Test RFID reader"""
    print("\nSmartt - Test RFID Reader")
    print("="*60)

    try:
        from src.rfid_reader import RFIDReader

        print("Connecting to RFID reader...")
        reader = RFIDReader()

        if not reader.connect():
            print("✗ Failed to connect to RFID reader")
            print("\nAvailable ports:")
            for port, desc in RFIDReader.list_available_ports():
                print(f"  - {port}: {desc}")
            return 1

        print(f"✓ Connected to {reader.port}")
        print("\nWaiting for card scan (30 seconds)...")
        print("Please scan an RFID card now...\n")

        card_id = reader.read_card(timeout=30)

        if card_id:
            print(f"✓ Card detected: {card_id}")
            print(f"\nReader is working correctly!")
            reader.disconnect()
            return 0
        else:
            print("✗ No card detected (timeout)")
            print("\nPossible issues:")
            print("  - RFID reader not configured correctly")
            print("  - Wrong serial port or baud rate")
            print("  - No card was scanned")
            reader.disconnect()
            return 1

    except Exception as e:
        print(f"Error: {e}")
        return 1


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Smartt Linux Service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --gui              Run with graphical interface
  %(prog)s --console          Run in console mode
  %(prog)s --http-server      Run HTTP server (Docker deployment)
  %(prog)s --status           Show service status
  %(prog)s --login            Login and save authentication token
  %(prog)s --sync             Sync offline queue
  %(prog)s --test-reader      Test RFID reader connection
        """
    )

    # Operation modes
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--gui', action='store_true',
                           help='Run with GUI (default)')
    mode_group.add_argument('--console', action='store_true',
                           help='Run in console mode (no GUI)')
    mode_group.add_argument('--http-server', action='store_true',
                           help='Run HTTP server for Docker deployment')
    mode_group.add_argument('--status', action='store_true',
                           help='Show service status')
    mode_group.add_argument('--login', action='store_true',
                           help='Login and save authentication token')
    mode_group.add_argument('--sync', action='store_true',
                           help='Manually sync offline queue')
    mode_group.add_argument('--test-reader', action='store_true',
                           help='Test RFID reader connection')

    # Options
    parser.add_argument('--mock-api', action='store_true',
                       help='Use mock API for testing')
    parser.add_argument('--config', type=str,
                       help='Path to configuration file')
    parser.add_argument('--log-level', type=str,
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                       help='Logging level')
    parser.add_argument('--log-file', type=str,
                       help='Path to log file')

    args = parser.parse_args()

    # Load configuration if specified
    if args.config:
        Config.load_from_file(args.config)

    # Setup logging
    setup_logging(log_level=args.log_level, log_file=args.log_file)
    logger = logging.getLogger(__name__)

    # Determine mode
    if args.status:
        return run_status_command()
    elif args.login:
        return run_login_command()
    elif args.sync:
        return run_sync_command()
    elif args.test_reader:
        return run_test_reader_command()
    else:
        # Run service
        service_manager = ServiceManager(use_mock_api=args.mock_api)

        if args.console:
            return run_console_mode(service_manager)
        elif args.http_server:
            return run_http_server_mode(service_manager)
        else:
            # Default to GUI mode
            return run_gui_mode(service_manager)


if __name__ == "__main__":
    sys.exit(main())
