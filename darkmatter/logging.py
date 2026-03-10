"""
Centralized logging for DarkMatter.

All components should use get_logger(source) instead of print(..., file=sys.stderr).
Dual output: RotatingFileHandler → ~/.darkmatter/darkmatter.log + StreamHandler → stderr.

Depends on: nothing (leaf module)
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_initialized = False
_LOG_DIR = os.path.join(os.path.expanduser("~"), ".darkmatter")
_LOG_FILE = os.path.join(_LOG_DIR, "darkmatter.log")
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_BACKUP_COUNT = 3


class _StderrFormatter(logging.Formatter):
    """Stderr format: [DarkMatter] [source] message — backward compat with old print() style."""

    def format(self, record: logging.LogRecord) -> str:
        # Extract the child logger name (e.g. "darkmatter.mesh" → "mesh")
        source = record.name.removeprefix("darkmatter.")
        return f"[DarkMatter] [{source}] {record.getMessage()}"


class _FileFormatter(logging.Formatter):
    """File format: [2026-03-10 04:47:56] [darkmatter.mesh] [INFO] message"""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return f"[{ts}] [{record.name}] [{record.levelname}] {record.getMessage()}"


def _ensure_initialized() -> None:
    """Lazy init — configure root 'darkmatter' logger on first use."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger("darkmatter")
    root.setLevel(logging.DEBUG)
    # Prevent propagation to the root logger (avoids duplicate output)
    root.propagate = False

    # File handler — rotating, all levels
    try:
        fh = RotatingFileHandler(
            _LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_FileFormatter())
        root.addHandler(fh)
    except OSError:
        pass  # Best-effort — don't crash if log dir is unwritable

    # Stderr handler — INFO and above
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(_StderrFormatter())
    root.addHandler(sh)


def get_logger(source: str) -> logging.Logger:
    """Get a child logger under the 'darkmatter' namespace.

    Usage:
        from darkmatter.logging import get_logger
        _log = get_logger("mesh")
        _log.info("Connected to peer %s", peer_id[:12])
    """
    _ensure_initialized()
    return logging.getLogger(f"darkmatter.{source}")
