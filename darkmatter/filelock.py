"""Small cross-process lock used to serialize one project's mailbox."""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class _LockState:
    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.handle = None


_registry_guard = threading.Lock()
_registry: dict[str, _LockState] = {}


def _state_for(path: Path) -> _LockState:
    key = str(path.resolve())
    with _registry_guard:
        return _registry.setdefault(key, _LockState())


def _lock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProjectLock:
    """Re-entrant in-process and exclusive cross-process file lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._state = _state_for(self.path)

    @contextmanager
    def acquire(self) -> Iterator[None]:
        with self._state.thread_lock:
            if self._state.depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.open("a+b")
                try:
                    _lock(handle)
                except Exception:
                    handle.close()
                    raise
                self._state.handle = handle
            self._state.depth += 1
            try:
                yield
            finally:
                self._state.depth -= 1
                if self._state.depth == 0:
                    handle = self._state.handle
                    self._state.handle = None
                    if handle is not None:
                        try:
                            _unlock(handle)
                        finally:
                            handle.close()


__all__ = ["ProjectLock"]
