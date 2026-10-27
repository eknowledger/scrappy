"""Crash-safe file replacement with temp files that cannot be hijacked and that work on NTFS/FUSE drives.

* The temp file has a random, fixed-length name (``.scrappy-<16 hex>.tmp``) in the target's folder and is created with
  O_EXCL | O_NOFOLLOW, so a symbolic link planted at a guessable name by someone with write access to the folder
  (a shared ROM folder) is never followed, and a file name close to the 255-byte limit still works.
* Copying the file mode and fsync are best effort: NTFS/FUSE mounts (``/media/Windows``) refuse chmod or fsync with
  EPERM/EINVAL/ENOTSUP/ENOSYS. Those errors are ignored; real I/O errors (EIO, ENOSPC, EDQUOT, ...) still propagate.
"""
from __future__ import annotations

import errno
import os
import secrets
import shutil
from pathlib import Path
from typing import BinaryIO, Callable, Optional

TOLERATED_ERRNOS = frozenset(getattr(errno, n) for n in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EINVAL", "ENOSYS")
                             if hasattr(errno, n))


def _best_effort(fn, *args):
    """Call fn(*args); ignore only the 'this filesystem cannot do that' errors."""
    try:
        fn(*args)
    except OSError as e:
        if e.errno not in TOLERATED_ERRNOS:
            raise


def fsync_dir(path):
    """Make a new or renamed directory entry durable. Best effort on filesystems that cannot fsync a folder."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return                                               # cannot even open the folder for reading: nothing to sync
    try:
        _best_effort(os.fsync, fd)
    finally:
        os.close(fd)


def create_temp(target) -> tuple[int, Path]:
    """Create an empty temp file next to `target` (fixed-length random name); return (fd, path)."""
    target = Path(target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(100):
        tmp = target.with_name(f".scrappy-{secrets.token_hex(8)}.tmp")
        try:
            return os.open(tmp, flags, 0o666), tmp           # the umask trims the mode like a normal new file
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create a temporary file next to {target}")


def atomic_replace(target, fill: Callable[[BinaryIO], None], *, mode: Optional[int] = None):
    """Write `target` through `fill(file)` so that it is either untouched or complete: temp file, fsync, rename.

    The new file keeps the mode of the file it replaces, or `mode` if given (best effort, see the module docstring).
    `target` is used as given (callers resolve symlinks first when they want the link kept).
    """
    target = Path(target)
    fd, tmp = create_temp(target)
    try:
        with os.fdopen(fd, "wb") as f:
            fill(f)
            f.flush()
            _best_effort(os.fsync, f.fileno())
        if mode is not None:
            _best_effort(os.chmod, tmp, mode)
        elif target.exists():
            _best_effort(shutil.copymode, target, tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
