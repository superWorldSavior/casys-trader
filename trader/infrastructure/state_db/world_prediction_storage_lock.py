"""Cooperative flock lease for World Model prediction hot/cold storage.

Writer/runtime callers take a shared lease with ``create=True``. Offline
cutover takes an exclusive lease on the same lock file. Reader helpers in
``world_prediction_tiers`` do not acquire this lease: the QUERY caller must
hold one spanning SQLite open, transaction, and close.
"""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path


class PredictionStorageBusyError(RuntimeError):
    """The storage lock could not be acquired, or a cutover journal is present."""


def storage_lock_path(db_path: str | Path) -> Path:
    path = Path(db_path)
    return path.with_name(path.name + ".storage.lock")


def cutover_journal_path(db_path: str | Path) -> Path:
    path = Path(db_path)
    return path.with_name(path.name + ".cutover.json")


def prediction_storage_lease(
    db_path: str | Path,
    *,
    exclusive: bool = False,
    create: bool = False,
    allow_in_progress: bool = False,
) -> "PredictionStorageLease":
    """Acquire a non-blocking flock lease next to ``db_path``.

    ``create=False`` never creates the lock file or parent directories. A missing
    lock is a legacy shared no-op lease, but exclusive ownership is refused and
    the cutover journal is still refused.
    """

    path = Path(db_path)
    lock_path = storage_lock_path(path)
    _assert_journal_idle(path, allow_in_progress=allow_in_progress)
    if not create and not _lexists(lock_path):
        _assert_journal_idle(path, allow_in_progress=allow_in_progress)
        if exclusive:
            raise PredictionStorageBusyError(f"world-model storage lock is missing: {lock_path}")
        return PredictionStorageLease(None)

    if create:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PredictionStorageBusyError(
                f"world-model storage lock directory unavailable: {lock_path.parent}"
            ) from exc

    flags = os.O_NOFOLLOW | (os.O_RDWR if exclusive or create else os.O_RDONLY)
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except FileNotFoundError as exc:
        if exclusive or create:
            raise PredictionStorageBusyError(f"world-model storage lock unavailable: {lock_path}") from exc
        _assert_journal_idle(path, allow_in_progress=allow_in_progress)
        return PredictionStorageLease(None)
    except OSError as exc:
        raise PredictionStorageBusyError(f"world-model storage lock unavailable: {lock_path}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PredictionStorageBusyError(f"world-model storage lock is not a regular file: {lock_path}")
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, operation)
        except (BlockingIOError, OSError) as exc:
            raise PredictionStorageBusyError(f"world-model storage lock is busy: {lock_path}") from exc
        _assert_journal_idle(path, allow_in_progress=allow_in_progress)
    except Exception:
        os.close(fd)
        raise
    return PredictionStorageLease(fd)


def shared_prediction_storage_lease(db_path: str | Path, *, create: bool = False) -> "PredictionStorageLease":
    return prediction_storage_lease(db_path, exclusive=False, create=create, allow_in_progress=False)


def exclusive_prediction_storage_lease(
    db_path: str | Path,
    *,
    allow_in_progress: bool = False,
) -> "PredictionStorageLease":
    return prediction_storage_lease(db_path, exclusive=True, create=True, allow_in_progress=allow_in_progress)


class PredictionStorageLease:
    """Holds one flock fd. ``close()`` is idempotent."""

    def __init__(self, fd: int | None) -> None:
        self._fd = fd
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "PredictionStorageLease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PredictionStorageBusyError(f"world-model storage path is unavailable: {path}") from exc
    return True


def _assert_journal_idle(db_path: Path, *, allow_in_progress: bool) -> None:
    journal = cutover_journal_path(db_path)
    if _lexists(journal) and not allow_in_progress:
        raise PredictionStorageBusyError(f"world-model cutover journal is in progress: {journal}")


__all__ = [
    "PredictionStorageBusyError",
    "PredictionStorageLease",
    "cutover_journal_path",
    "exclusive_prediction_storage_lease",
    "prediction_storage_lease",
    "shared_prediction_storage_lease",
    "storage_lock_path",
]
