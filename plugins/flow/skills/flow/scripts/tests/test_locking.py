import pytest

from _locking import LockContention, flock_blocking, flock_retry


def test_flock_blocking_roundtrip(tmp_path):
    lock = tmp_path / "x.lock"
    with flock_blocking(lock):
        assert lock.exists()
    with flock_blocking(lock):
        pass


def test_flock_retry_acquires_when_free(tmp_path):
    lock = tmp_path / "x.lock"
    with flock_retry(lock, retries=1, delay=0.01):
        pass


def test_flock_retry_raises_when_held(tmp_path):
    lock = tmp_path / "x.lock"
    with (
        flock_blocking(lock),
        pytest.raises(LockContention),
        flock_retry(lock, retries=2, delay=0.01),
    ):
        pass


def test_flock_retry_releases_for_next_acquirer(tmp_path):
    lock = tmp_path / "x.lock"
    with flock_retry(lock, retries=1, delay=0.01):
        pass
    with flock_retry(lock, retries=1, delay=0.01):
        pass
