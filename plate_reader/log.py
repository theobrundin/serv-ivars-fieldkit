"""Logging setup. One place to configure formatters, levels, handlers."""
import errno
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path("/var/log/serv")
LOG_FILE = LOG_DIR / "plate_reader.log"

_configured = False


def setup(level: str = "INFO") -> None:
    """Idempotent. Safe to call from any module before first use."""
    global _configured
    if _configured:
        return

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Confined read-only helpers can import config without a writable log
        # directory. Journald remains available; unexpected I/O failures do not
        # silently disappear (for example, a full or damaged filesystem).
        if not isinstance(exc, PermissionError) and exc.errno != errno.EROFS:
            raise

    fmt = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = __import__("time").gmtime  # always UTC

    root = logging.getLogger()
    root.setLevel(level)

    # stderr → journald via systemd
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # rotating file (keep 5 × 5MB)
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        if (not isinstance(exc, (PermissionError, FileNotFoundError))
                and exc.errno != errno.EROFS):
            raise
        # Journald is enough when the service sandbox keeps this path read-only.

    # Quiet some chatty libs
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("picamera2").setLevel(logging.WARNING)

    _configured = True


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
