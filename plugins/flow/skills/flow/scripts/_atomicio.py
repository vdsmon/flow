"""Shared atomic file writes: temp + fsync + os.replace + parent-dir fsync.

state.py grew this inline first; recall_pending.py copied it; lease.py and
snapshot.py would be the third and fourth. This is the one copy. The parent-dir
fsync makes the rename itself durable across a crash.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp makes the temp 0o600. An explicit mode lets generated executables publish their
        # content and executable bit in one rename; otherwise preserve the destination's mode, with
        # 0o644 for a new file.
        target_mode = mode
        if target_mode is None:
            try:
                target_mode = stat.S_IMODE(os.stat(path).st_mode)
            except FileNotFoundError:
                target_mode = 0o644
        if mode is None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, target_mode)
        else:
            os.chmod(tmp, target_mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def atomic_write_text(
    path: Path, text: str, encoding: str = "utf-8", *, mode: int | None = None
) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


__all__ = ["atomic_write_bytes", "atomic_write_text"]
