"""Shared POSIX advisory file locks (fcntl.flock).

One place for the lock primitive that state.py, memory_append.py and
observe_ship_event.py each used to copy. Two flavors:

- flock_blocking: LOCK_EX, waits indefinitely. For single-process-critical
  sections that must serialize (per-ticket state writes).
- flock_retry: LOCK_EX | LOCK_NB with bounded retry, raising LockContention on
  exhaustion. For multi-writer append paths that prefer to fail fast and let the
  caller map contention to its own exit code.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_RETRY_COUNT = 3
LOCK_RETRY_DELAY_S = 1.0


class LockContention(Exception):
    """Raised by flock_retry when the lock could not be acquired in time."""


@contextmanager
def flock_blocking(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive blocking flock on `lock_path` for the with-block."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def flock_retry(
    lock_path: Path,
    retries: int = LOCK_RETRY_COUNT,
    delay: float = LOCK_RETRY_DELAY_S,
) -> Iterator[None]:
    """Hold an exclusive flock, retrying non-blocking acquisition `retries` times.

    Raises LockContention if the lock is still held after the final attempt.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        for attempt in range(retries):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if attempt == retries - 1:
                    raise LockContention(
                        f"could not lock {lock_path} after {retries} attempts"
                    ) from None
                time.sleep(delay)
        if not acquired:
            raise LockContention(f"lock loop exited without lock on {lock_path}")
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


__all__ = [
    "LOCK_RETRY_COUNT",
    "LOCK_RETRY_DELAY_S",
    "LockContention",
    "flock_blocking",
    "flock_retry",
]
