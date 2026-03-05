"""
Cross-platform file locking (Unix fcntl / Windows msvcrt).

Usage:
    from darkmatter.filelock import lock_exclusive, lock_shared, unlock, lock_exclusive_nb

All functions take a file object (or file descriptor).
"""

import os
import sys

if sys.platform == "win32":
    import msvcrt

    def lock_exclusive(f):
        """Blocking exclusive lock."""
        fd = f.fileno() if hasattr(f, "fileno") else f
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def lock_shared(f):
        """Shared lock (Windows has no true shared lock — falls back to exclusive)."""
        lock_exclusive(f)

    def lock_exclusive_nb(f):
        """Non-blocking exclusive lock. Raises OSError if already locked."""
        fd = f.fileno() if hasattr(f, "fileno") else f
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlock(f):
        """Release lock."""
        fd = f.fileno() if hasattr(f, "fileno") else f
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # Unlock may fail if fd was already closed or never locked; safe to ignore

else:
    import fcntl

    def lock_exclusive(f):
        fd = f.fileno() if hasattr(f, "fileno") else f
        fcntl.flock(fd, fcntl.LOCK_EX)

    def lock_shared(f):
        fd = f.fileno() if hasattr(f, "fileno") else f
        fcntl.flock(fd, fcntl.LOCK_SH)

    def lock_exclusive_nb(f):
        fd = f.fileno() if hasattr(f, "fileno") else f
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(f):
        fd = f.fileno() if hasattr(f, "fileno") else f
        fcntl.flock(fd, fcntl.LOCK_UN)
