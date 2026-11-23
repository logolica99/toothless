"""Owner-private singleton lock for Telegram polling."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path


class ControllerAlreadyRunningError(RuntimeError):
    pass


def acquire_controller_lock(database: Path) -> int:
    directory = database.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("controller state directory must not be a symlink")
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("controller state directory must be owner-only")
    path = directory / "controller.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise ControllerAlreadyRunningError("another Toothless controller holds the polling lock") from exc
    return descriptor
