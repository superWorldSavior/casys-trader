"""Archive et purge prudemment les donnees generees des agents Ops.

Trois classes de données, trois traitements :

- **sessions agents** : les sessions ACPX fermees et les repertoires de session
  Grok inactifs sont archives comme unites de cycle de vie completes. Les
  rollouts Codex et les sessions Kimi restent explicitement proteges tant que
  leur contrat de retention n'est pas demontre.
- **logs applicatifs** (`ops/codex-home/logs_*.sqlite`) : traces INFO/TRACE du
  process app-server. Aucune valeur post-exécution — purge sèche. Avant la
  purge, ``scripts.llm_cost`` extrait tout usage LLM trouvable (sqlite,
  sessions acpx, homes grok, fallback ``[acpx_call]``) vers
  ``state/archive/llm_usage/``.
- **configuration, identite, index actifs et sessions recentes** : jamais
  archives par inference. Les homes inconnus sont signales et proteges.

`--dry-run` est le défaut : l'écriture exige `--apply` (AX #2, safe defaults).
La sortie est un objet JSON (AX #3) pour être exploitable par un agent.

Usage::

    python -m scripts.archive_agent_storage                 # dry-run, tout
    python -m scripts.archive_agent_storage --apply
    python -m scripts.archive_agent_storage --older-than 14 --apply
    python -m scripts.archive_agent_storage --only sessions --apply
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from trader.domain.agent_storage_retention import (
    ArchiveSource,
    ArchiveUnit,
    ObservedQuarantineUnit,
    PendingReconciliationJournal,
    PurgeSource,
    QUARANTINE_DIR_NAME,
    QuarantineDeletionAuthority,
    QuarantineRecoveryKind,
    QuarantineTransactionIntent,
    QuarantineUnitIntent,
    SessionDocsVacuumJournal,
    TransactionRecoveryPlan,
    acpx_stream_lock_is_recoverable,
    decide_transaction_recovery,
    is_lexically_confined,
    planned_quarantine_path,
    provider_for_source_id,
    quarantine_intent_dir,
    quarantine_root,
    session_docs_vacuum_journal_path,
)

DEFAULT_OLDER_THAN_DAYS = 7
ZSTD_LEVEL = "19"
APPLY_LOCK_NAME = "apply.lock"
PENDING_JOURNAL_NAME = "pending-reconciliation.json"

# launchd/cron n'héritent pas du PATH interactif : résoudre l'exécutable
# explicitement plutôt que d'échouer au milieu d'une archive.
_ZSTD_FALLBACK_PATHS = ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", "/usr/bin/zstd")
_ACPX_FALLBACK_PATHS = ("/opt/homebrew/bin/acpx", "/usr/local/bin/acpx")

ACPX_SESSIONS = Path.home() / ".acpx" / "sessions"
REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = Path.home() / ".acpx" / "archive"
CODEX_HOME = REPO_ROOT / "ops" / "codex-home"
OPS_ROOT = REPO_ROOT / "ops"

# Ne jamais archiver : l'index vivant et les verrous de sessions actives.
PROTECTED_NAMES = {"index.json"}
PROTECTED_SUFFIXES = {".lock", ".tmp"}

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)


def agent_storage_policy(ops_root: Path | None = None) -> tuple[tuple[ArchiveSource, ...], tuple[PurgeSource, ...]]:
    """Return the complete, conservative policy for known homes under ``ops``."""

    ops_root = OPS_ROOT if ops_root is None else ops_root
    archives = (
        ArchiveSource(
            "acpx",
            ACPX_SESSIONS,
            "sessions",
            "acpx_sessions",
            authority_root=ACPX_SESSIONS,
            allows_external_root=True,
        ),
        ArchiveSource(
            "codex-home",
            ops_root / "codex-home" / "sessions",
            "sessions",
            "recursive_files",
            apply_enabled=False,
            protection_reason="state_5.sqlite references rollout_path; composite retention is not implemented",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "codex-home",
            ops_root / "codex-home" / "shell_snapshots",
            "snapshots",
            "recursive_files",
            apply_enabled=False,
            protection_reason="Codex runtime history lifecycle is not yet provider-safe",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "grok-home",
            ops_root / "grok-home" / "sessions",
            "sessions",
            "grok_sessions",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "grok-home",
            ops_root / "grok-home" / "logs",
            "logs",
            "recursive_files",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "grok-home",
            ops_root / "grok-home" / "memtrace",
            "traces",
            "recursive_files",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "grok-home-medium",
            ops_root / "grok-home-medium" / "sessions",
            "sessions",
            "grok_sessions",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "grok-home-medium",
            ops_root / "grok-home-medium" / "logs",
            "logs",
            "recursive_files",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "grok-home-medium",
            ops_root / "grok-home-medium" / "memtrace",
            "traces",
            "recursive_files",
            authority_root=ops_root,
        ),
        # Kimi has no generated session tree today. Declared so inventory sees
        # the conventional paths; apply stays disabled until the lifecycle
        # contract is known (report-only, never auto-archive).
        ArchiveSource(
            "kimi-home",
            ops_root / "kimi-home" / "sessions",
            "sessions",
            "recursive_files",
            apply_enabled=False,
            protection_reason="Kimi session lifecycle contract is not yet known",
            authority_root=ops_root,
        ),
        ArchiveSource(
            "kimi-home",
            ops_root / "kimi-home" / "logs",
            "logs",
            "recursive_files",
            apply_enabled=False,
            protection_reason="Kimi log lifecycle contract is not yet known",
            authority_root=ops_root,
        ),
    )
    purges = (
        PurgeSource("codex-home", ops_root / "codex-home" / "cache", authority_root=ops_root),
        PurgeSource("codex-home", ops_root / "codex-home" / "tmp", authority_root=ops_root),
        PurgeSource("kimi-home", ops_root / "kimi-home" / "cache", authority_root=ops_root),
        PurgeSource("kimi-home", ops_root / "kimi-home" / "tmp", authority_root=ops_root),
    )
    return archives, purges


def resolve_zstd() -> str:
    """Chemin de l'exécutable zstd, ou RuntimeError explicite (AX #5, fast fail)."""

    found = shutil.which("zstd")
    if found:
        return found
    for candidate in _ZSTD_FALLBACK_PATHS:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "zstd introuvable (PATH et chemins connus). Installer avec `brew install zstd`, "
        "ou ajouter son répertoire au PATH du job planifié."
    )


def _human(size: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} Go"


def _month_key(path: Path) -> str:
    ts = datetime.fromtimestamp(os.lstat(path).st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m")


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _is_dir_nofollow(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _is_file_nofollow(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _confined_nofollow(path: Path, authority: Path) -> bool:
    """Reject any symlink on the authority→path walk and any lexical escape."""

    if not is_lexically_confined(path, authority):
        return False
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    root = Path(os.path.normpath(os.path.abspath(authority)))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    current = root
    try:
        if stat.S_ISLNK(os.lstat(current).st_mode):
            return False
    except OSError:
        return False
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return False
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return False
        except OSError:
            return False
    return True


_DIR_FD_FUNCS = frozenset({"open", "unlink", "rmdir", "mkdir", "rename", "lstat"})


def _require_directory_fd_primitives() -> None:
    """Fail closed when the platform cannot hold a directory fd without following."""

    missing = [name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC") if not hasattr(os, name)]
    if missing:
        raise RuntimeError("directory fd no-follow primitives unavailable: " + ",".join(missing))
    supported = getattr(os, "supports_dir_fd", None)
    if supported is None:
        raise RuntimeError("os.supports_dir_fd is unavailable on this platform")
    names = {getattr(func, "__name__", "") for func in supported}
    missing_funcs = sorted(_DIR_FD_FUNCS - names)
    if missing_funcs:
        raise RuntimeError("dir_fd unsupported on this platform: " + ",".join(missing_funcs))
    fd_names = {getattr(func, "__name__", "") for func in getattr(os, "supports_fd", set())}
    if "listdir" not in fd_names:
        raise RuntimeError("os.listdir(fd) is unavailable on this platform")


def _directory_fd_flags() -> int:
    _require_directory_fd_primitives()
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_fd_flags(extra: int = 0) -> int:
    _require_directory_fd_primitives()
    return extra | os.O_NOFOLLOW | os.O_CLOEXEC


def _raise_if_symlink_open_failed(path: Path | str, exc: OSError, *, dir_fd: int | None = None) -> None:
    name = os.fsdecode(path) if not isinstance(path, Path) else str(path)
    try:
        st = os.lstat(name, dir_fd=dir_fd) if dir_fd is not None else os.lstat(name)
    except OSError:
        raise exc from None
    if stat.S_ISLNK(st.st_mode) or exc.errno == errno.ELOOP:
        raise RuntimeError(f"refusing symlink in directory chain: {name}") from exc


def _open_directory_nofollow(path: Path | str, *, dir_fd: int | None = None) -> int:
    """Open a directory with O_DIRECTORY|O_NOFOLLOW and revalidate the inode."""

    flags = _directory_fd_flags()
    name = str(path) if dir_fd is None else os.fsdecode(path)
    try:
        st = os.lstat(name, dir_fd=dir_fd) if dir_fd is not None else os.lstat(name)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink in directory chain: {name}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"not a directory: {name}")
    try:
        fd = os.open(name, flags, dir_fd=dir_fd) if dir_fd is not None else os.open(name, flags)
    except OSError as exc:
        _raise_if_symlink_open_failed(name, exc, dir_fd=dir_fd)
        raise
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISDIR(fst.st_mode):
            raise RuntimeError(f"directory inode changed: {name}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_regular_nofollow(name: str, *, dir_fd: int, flags: int) -> int:
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink in directory chain: {name}")
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"not a regular file: {name}")
    try:
        fd = os.open(name, _file_fd_flags(flags), dir_fd=dir_fd)
    except OSError as exc:
        _raise_if_symlink_open_failed(name, exc, dir_fd=dir_fd)
        raise
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(fst.st_mode):
            raise RuntimeError(f"file inode changed: {name}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _held_dir_matches_path(path: Path, fd: int) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    held = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino)


def _assert_real_directory(path: Path, *, what: str) -> None:
    if _is_symlink(path):
        raise RuntimeError(f"refusing symlink {what}: {path}")
    if not _is_dir_nofollow(path):
        raise RuntimeError(f"{what} is not a directory: {path}")
    fd = _open_directory_nofollow(path)
    os.close(fd)


def _ensure_real_archive_root(root: Path) -> None:
    """Create or reopen the archive root without following a symlink leaf or ancestor."""

    root = Path(os.path.normpath(os.path.abspath(root)))
    if _is_symlink(root):
        raise RuntimeError(f"refusing symlink archive root: {root}")
    if _path_exists_nofollow(root):
        _assert_real_directory(root, what="archive root")
        os.chmod(root, 0o700, follow_symlinks=False)
        return
    missing: list[Path] = []
    current = root
    while True:
        try:
            st = os.lstat(current)
            break
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise RuntimeError(f"missing archive root ancestor: {root}") from None
            current = parent
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink archive ancestor: {current}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"archive ancestor is not a directory: {current}")
    fd = _open_directory_nofollow(current)
    try:
        for path in reversed(missing):
            try:
                os.mkdir(path.name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child_st = os.lstat(path.name, dir_fd=fd)
            if stat.S_ISLNK(child_st.st_mode):
                raise RuntimeError(f"refusing symlink archive root: {path}")
            if not stat.S_ISDIR(child_st.st_mode):
                raise RuntimeError(f"archive root is not a directory: {path}")
            child_fd = _open_directory_nofollow(path.name, dir_fd=fd)
            os.close(fd)
            fd = child_fd
            os.chmod(path, 0o700, follow_symlinks=False)
    finally:
        os.close(fd)


def _unusable_declared_root(source: ArchiveSource) -> str | None:
    authority = source.authority_root
    if authority is None:
        return "unconfined declared root"
    try:
        os.lstat(source.root)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"unreadable declared root:{exc}"
    if _is_symlink(source.root):
        return "declared root is a symlink"
    if not _is_dir_nofollow(source.root):
        return "declared root is not a directory"
    if not _confined_nofollow(source.root, authority):
        return "declared root is not confined (symlink in authority path)"
    return None


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RetentionLockUnavailable(Exception):
    """Another apply owns the process-wide retention lock, or the lock cannot be proven."""

    def __init__(self, message: str, *, state: dict[str, object]) -> None:
        super().__init__(message)
        self.state = state


class QuarantineRecoveryError(RuntimeError):
    """A leftover quarantine transaction is corrupt or conflicts with live data."""


def _retention_state_dir() -> Path:
    return REPO_ROOT / "state" / "retention"


def _apply_lock_path() -> Path:
    return _retention_state_dir() / APPLY_LOCK_NAME


def _pending_journal_path() -> Path:
    return _retention_state_dir() / PENDING_JOURNAL_NAME


def _lock_state(*, held_by_other: bool, acquired: bool, error: str | None = None) -> dict[str, object]:
    state: dict[str, object] = {
        "path": str(_apply_lock_path()),
        "held_by_other": held_by_other,
        "acquired": acquired,
    }
    if error:
        state["error"] = error
    return state


def _probe_retention_lock() -> dict[str, object]:
    path = _apply_lock_path()
    try:
        if _is_symlink(path):
            return _lock_state(held_by_other=False, acquired=False, error="retention lock is a symlink")
        if not _is_file_nofollow(path):
            return _lock_state(held_by_other=False, acquired=False)
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _lock_state(held_by_other=False, acquired=False)
    except OSError as exc:
        return _lock_state(held_by_other=False, acquired=False, error=f"{type(exc).__name__}:{exc}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return _lock_state(held_by_other=False, acquired=False)
    except BlockingIOError:
        return _lock_state(held_by_other=True, acquired=False)
    finally:
        os.close(fd)


@contextmanager
def _hold_retention_apply_lock():
    state_dir = _retention_state_dir()
    if state_dir.exists() and _is_symlink(state_dir):
        raise RetentionLockUnavailable(
            "retention state directory is a symlink",
            state=_lock_state(held_by_other=False, acquired=False, error="retention state directory is a symlink"),
        )
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _is_symlink(state_dir) or not _is_dir_nofollow(state_dir):
        raise RetentionLockUnavailable(
            "retention state directory is a symlink",
            state=_lock_state(held_by_other=False, acquired=False, error="retention state directory is a symlink"),
        )
    os.chmod(state_dir, 0o700)
    lock_path = _apply_lock_path()
    if lock_path.exists() and _is_symlink(lock_path):
        raise RetentionLockUnavailable(
            "retention apply lock is a symlink",
            state=_lock_state(held_by_other=False, acquired=False, error="retention apply lock is a symlink"),
        )
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RetentionLockUnavailable(
            f"retention apply lock unavailable:{type(exc).__name__}:{exc}",
            state=_lock_state(held_by_other=False, acquired=False, error=f"{type(exc).__name__}:{exc}"),
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetentionLockUnavailable(
                "retention apply lock unavailable",
                state=_lock_state(held_by_other=True, acquired=False),
            ) from exc
        os.ftruncate(fd, 0)
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()},
            separators=(",", ":"),
        ).encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
        os.chmod(lock_path, 0o600)
        yield _lock_state(held_by_other=False, acquired=True)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def load_pending_journal() -> PendingReconciliationJournal:
    path = _pending_journal_path()
    if _is_symlink(path):
        raise RuntimeError("pending reconciliation journal is a symlink")
    if not _is_file_nofollow(path):
        return PendingReconciliationJournal()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PendingReconciliationJournal.from_payload(payload)


def save_pending_journal(journal: PendingReconciliationJournal) -> None:
    state_dir = _retention_state_dir()
    if state_dir.exists() and _is_symlink(state_dir):
        raise RuntimeError("retention state directory is a symlink")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    path = _pending_journal_path()
    if path.exists() and _is_symlink(path):
        raise RuntimeError("pending reconciliation journal is a symlink")
    if not journal.entries and journal.last_error is None:
        if _is_file_nofollow(path):
            path.unlink()
            _fsync_dir(state_dir)
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=state_dir,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(
                json.dumps(journal.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        _fsync_file(path)
        _fsync_dir(state_dir)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json_atomically(path: Path, payload: dict[str, object], *, directory: Path) -> None:
    if directory.exists() and _is_symlink(directory):
        raise RuntimeError(f"{directory} is a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if path.exists() and _is_symlink(path):
        raise RuntimeError(f"{path} is a symlink")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        _fsync_file(path)
        _fsync_dir(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_quarantine_intent(intent: QuarantineTransactionIntent) -> None:
    payload = intent.to_payload()
    for guard in intent.guard_roots():
        quarantine = quarantine_root(guard)
        intent_dir = quarantine_intent_dir(guard)
        if quarantine.exists() and _is_symlink(quarantine):
            raise RuntimeError("quarantine directory is a symlink")
        if intent_dir.exists() and _is_symlink(intent_dir):
            raise RuntimeError("quarantine intent directory is a symlink")
        quarantine.mkdir(mode=0o700, exist_ok=True)
        os.chmod(quarantine, 0o700)
        _fsync_dir(quarantine)
        _write_json_atomically(
            intent_dir / f"{intent.transaction_id}.json",
            payload,
            directory=intent_dir,
        )
        _fsync_dir(quarantine)


def drop_quarantine_intent(intent: QuarantineTransactionIntent) -> None:
    for guard in intent.guard_roots():
        intent_dir = quarantine_intent_dir(guard)
        path = intent_dir / f"{intent.transaction_id}.json"
        if _is_symlink(path):
            raise RuntimeError("quarantine intent is a symlink")
        if _is_file_nofollow(path):
            path.unlink()
            if _is_dir_nofollow(intent_dir):
                _fsync_dir(intent_dir)


def _path_exists_nofollow(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _iter_quarantine_intent_files(roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        intent_dir = quarantine_intent_dir(root)
        if _is_symlink(intent_dir):
            raise QuarantineRecoveryError("quarantine intent directory is a symlink")
        if not _is_dir_nofollow(intent_dir):
            continue
        try:
            with os.scandir(intent_dir) as scanned:
                children = list(scanned)
        except OSError as exc:
            raise QuarantineRecoveryError(f"quarantine intent unreadable:{type(exc).__name__}:{exc}") from exc
        for entry in children:
            child = Path(entry.path)
            if entry.name.startswith(".") or not entry.name.endswith(".json"):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise QuarantineRecoveryError("quarantine intent is a symlink")
            found.append(child)
    return found


def load_quarantine_intents(roots: Iterable[Path]) -> tuple[QuarantineTransactionIntent, ...]:
    loaded: dict[str, QuarantineTransactionIntent] = {}
    for path in _iter_quarantine_intent_files(roots):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            intent = QuarantineTransactionIntent.from_payload(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise QuarantineRecoveryError(f"quarantine intent unreadable:{type(exc).__name__}:{exc}") from exc
        existing = loaded.get(intent.transaction_id)
        if existing is not None and existing.to_payload() != intent.to_payload():
            raise QuarantineRecoveryError("quarantine intent conflict")
        loaded[intent.transaction_id] = intent
    return tuple(loaded.values())


def _observe_intent_unit(unit: QuarantineUnitIntent) -> ObservedQuarantineUnit:
    if _is_symlink(unit.live_path) or _is_symlink(unit.quarantine_path):
        return ObservedQuarantineUnit(
            unit=unit,
            live_exists=True,
            quarantine_exists=True,
            quarantine_matches_intent=False,
        )
    quarantine_exists = _path_exists_nofollow(unit.quarantine_path)
    matches = True
    if quarantine_exists:
        snapshot = _tree_snapshot(unit.quarantine_path)
        matches = snapshot is not None and snapshot[2] == unit.fingerprint
    return ObservedQuarantineUnit(
        unit=unit,
        live_exists=_path_exists_nofollow(unit.live_path),
        quarantine_exists=quarantine_exists,
        quarantine_matches_intent=matches,
    )


def _unverified_authority(journaled_identities: frozenset[str]) -> QuarantineDeletionAuthority:
    return QuarantineDeletionAuthority(
        archive_verified=False,
        published_archive=None,
        published_archive_sha256=None,
        journaled_identities=journaled_identities,
    )


def _assert_published_archive_authoritative(intent: QuarantineTransactionIntent) -> None:
    published = intent.published_archive
    expected = intent.published_archive_sha256
    if published is None or expected is None or not intent.archive_verified:
        raise RuntimeError("verified intent is missing published archive proof")
    if _is_symlink(published) or not _is_file_nofollow(published):
        raise RuntimeError("published archive is missing or is a symlink")
    if not _confined_nofollow(published, ARCHIVE_ROOT):
        raise RuntimeError("published archive escapes the archive root")
    digest = _sha256_file(published)
    if digest != expected:
        raise RuntimeError("published archive sha256 does not match the durable intent")
    with tempfile.TemporaryDirectory() as tmpdir:
        _verify_published_archive(
            published,
            [unit.as_archive_unit() for unit in intent.units],
            tmpdir=Path(tmpdir),
        )


def _authority_for_intent(
    intent: QuarantineTransactionIntent,
    journal: PendingReconciliationJournal,
) -> QuarantineDeletionAuthority:
    authority = intent.deletion_authority(journal)
    if not intent.archive_verified:
        return _unverified_authority(authority.journaled_identities)
    try:
        _assert_published_archive_authoritative(intent)
    except (OSError, RuntimeError):
        return _unverified_authority(authority.journaled_identities)
    return authority


def _rmdir_empty_parents(start: Path, stop: Path) -> None:
    parent = start
    seen: set[Path] = set()
    while parent not in seen and parent != stop and is_lexically_confined(parent, stop):
        seen.add(parent)
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _cleanup_quarantine_tree(intent: QuarantineTransactionIntent) -> None:
    for unit in intent.units:
        _rmdir_empty_parents(unit.quarantine_path.parent, quarantine_root(unit.guard_root))
    for guard in intent.guard_roots():
        intent_dir = quarantine_intent_dir(guard)
        if _is_dir_nofollow(intent_dir) and not _is_symlink(intent_dir):
            try:
                intent_dir.rmdir()
            except OSError:
                pass
        quarantine = quarantine_root(guard)
        if _is_dir_nofollow(quarantine) and not _is_symlink(quarantine):
            try:
                quarantine.rmdir()
            except OSError:
                pass


def recover_leftover_quarantine_transactions(
    roots: Iterable[Path],
    journal: PendingReconciliationJournal,
) -> list[dict[str, object]]:
    """Replay leftover quarantine intents under the caller-held apply lock."""

    intents = load_quarantine_intents(roots)
    decided: list[tuple[QuarantineTransactionIntent, TransactionRecoveryPlan]] = []
    for intent in intents:
        plan = decide_transaction_recovery(
            intent,
            tuple(_observe_intent_unit(unit) for unit in intent.units),
            _authority_for_intent(intent, journal),
        )
        if plan.kind is QuarantineRecoveryKind.FAIL_CLOSED:
            raise QuarantineRecoveryError(plan.reason)
        decided.append((intent, plan))

    reports: list[dict[str, object]] = []
    for intent, plan in decided:
        if plan.kind is QuarantineRecoveryKind.RESTORE:
            try:
                _restore_quarantined([unit.as_archive_unit() for unit in plan.restore])
            except (OSError, RuntimeError) as exc:
                raise QuarantineRecoveryError(f"quarantine restore failed:{exc}") from exc
            for unit in plan.restore:
                if not _path_exists_nofollow(unit.live_path) or _is_symlink(unit.live_path):
                    raise QuarantineRecoveryError("quarantine restore failed")
                snapshot = _tree_snapshot(unit.live_path)
                if snapshot is None or snapshot[2] != unit.fingerprint:
                    raise QuarantineRecoveryError("restored fingerprint does not match the durable intent")
        elif plan.kind is QuarantineRecoveryKind.FINALIZE:
            try:
                _assert_published_archive_authoritative(intent)
            except (OSError, RuntimeError) as exc:
                raise QuarantineRecoveryError(f"published archive re-verification failed:{exc}") from exc
            for unit in plan.finalize:
                if _path_exists_nofollow(unit.quarantine_path):
                    _remove_unit(unit.as_archive_unit())
        drop_quarantine_intent(intent)
        _cleanup_quarantine_tree(intent)
        reports.append(
            {
                "transaction_id": intent.transaction_id,
                "action": plan.kind.value,
                "reason": plan.reason,
                "units": len(intent.units),
            }
        )
    return reports


def _journal_report(journal: PendingReconciliationJournal) -> dict[str, object]:
    return {
        "schema_version": journal.schema_version,
        "pending_sources": [
            {
                "source_id": entry.source_id,
                "provider": entry.provider.value,
                "identities": list(entry.identities),
            }
            for entry in journal.entries
        ],
        "last_error": journal.last_error,
    }


def _tree_snapshot(path: Path) -> tuple[int, datetime, str] | None:
    """Return a metadata fingerprint, rejecting symlinks and unreadable trees."""

    try:
        if _is_symlink(path):
            return None
        root_stat = os.lstat(path)
        digest = hashlib.sha256()
        size = 0
        newest_ns = root_stat.st_mtime_ns
        entries: list[tuple[Path, os.stat_result]] = []
        if stat.S_ISREG(root_stat.st_mode):
            entries.append((path, root_stat))
            size = root_stat.st_size
        elif stat.S_ISDIR(root_stat.st_mode):
            stack = [path]
            while stack:
                current = stack.pop()
                with os.scandir(current) as scanned:
                    children = list(scanned)
                for entry in children:
                    child = Path(entry.path)
                    if entry.is_symlink():
                        return None
                    child_stat = entry.stat(follow_symlinks=False)
                    entries.append((child, child_stat))
                    newest_ns = max(newest_ns, child_stat.st_mtime_ns)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        size += child_stat.st_size
        else:
            return None
        for candidate, candidate_stat in sorted(entries, key=lambda item: str(item[0])):
            relative = candidate.name if candidate == path else str(candidate.relative_to(path))
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(str(candidate_stat.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(candidate_stat.st_mtime_ns).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(candidate_stat.st_ino).encode("ascii"))
            digest.update(b"\n")
        return size, datetime.fromtimestamp(newest_ns / 1_000_000_000, tz=timezone.utc), digest.hexdigest()
    except OSError:
        return None


def _active_session_ids(registry: Path) -> set[str] | None:
    """Read Grok's active registry; ``None`` means the safety proof failed."""

    if not _is_file_nofollow(registry) or _is_symlink(registry):
        return None
    lock_path = registry.with_name("active_sessions.lock")
    if not _is_file_nofollow(lock_path) or _is_symlink(lock_path):
        return None
    try:
        fd = os.open(str(lock_path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        try:
            return _active_session_ids_unlocked(registry)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        return None
    finally:
        os.close(fd)


def _active_session_ids_unlocked(registry: Path) -> set[str] | None:
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ids: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, str):
            if _SESSION_ID_RE.fullmatch(value):
                ids.add(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"id", "session_id", "chat_id"}:
                    visit(item)
                elif isinstance(item, (dict, list)):
                    visit(item)

    visit(payload)
    return ids


def _archive_unit(
    path: Path,
    *,
    source: ArchiveSource,
    identity: str | None = None,
    active_registry: Path | None = None,
) -> ArchiveUnit | None:
    if not _confined_nofollow(path, source.root):
        return None
    snapshot = _tree_snapshot(path)
    if snapshot is None:
        return None
    size, recorded_at, fingerprint = snapshot
    try:
        relative = path.relative_to(source.root)
    except ValueError:
        return None
    return ArchiveUnit(
        path=path,
        arcname=str(Path(source.source_id) / source.category / relative),
        recorded_at=recorded_at,
        size=size,
        identity=identity,
        fingerprint=fingerprint,
        guard_root=source.root,
        active_registry=active_registry,
    )


def _recorded_at_from_payload(payload: dict, fallback: datetime) -> datetime:
    for key in ("closed_at", "closedAt", "last_used_at", "lastUsedAt", "updated_at", "updatedAt"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
    return fallback


def _iter_regular_files_nofollow(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scanned:
                children = list(scanned)
        except OSError:
            continue
        for entry in children:
            child = Path(entry.path)
            if entry.name == QUARANTINE_DIR_NAME or entry.name.startswith("."):
                continue
            if entry.is_symlink():
                yield child, True
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(child)
            elif entry.is_file(follow_symlinks=False):
                yield child, False


def collect_source_units(
    source: ArchiveSource,
    cutoff: datetime,
) -> tuple[dict[str, list[ArchiveUnit]], list[dict[str, str]]]:
    """Collect complete lifecycle units; unknown or unsafe objects stay protected."""

    grouped: dict[str, list[ArchiveUnit]] = defaultdict(list)
    protected: list[dict[str, str]] = []
    unusable = _unusable_declared_root(source)
    if unusable == "missing":
        return grouped, protected
    if unusable is not None:
        protected.append({"path": str(source.root), "reason": unusable})
        return grouped, protected
    cutoff_ts = cutoff.timestamp()

    if not source.apply_enabled:
        protected.append(
            {
                "path": str(source.root),
                "reason": source.protection_reason or "source policy is report-only",
            }
        )
        return grouped, protected

    candidates: list[tuple[Path, str | None, Path | None, Path | None, datetime | None]] = []
    if source.layout == "acpx_sessions":
        for record in source.root.glob("*.json"):
            if record.name in PROTECTED_NAMES:
                continue
            if _is_symlink(record):
                protected.append({"path": str(record), "reason": "symlink"})
                continue
            try:
                payload = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                protected.append({"path": str(record), "reason": "unreadable ACPX record"})
                continue
            if not isinstance(payload, dict) or payload.get("closed") is not True:
                protected.append({"path": str(record), "reason": "ACPX session is not closed"})
                continue
            identity = str(payload.get("acpx_record_id") or record.stem).strip()
            members = [record]
            stream = source.root / f"{identity}.stream.ndjson"
            if _is_file_nofollow(stream) and not _is_symlink(stream):
                members.append(stream)
            elif _is_symlink(stream):
                protected.append({"path": str(stream), "reason": "symlink"})
                continue
            snapshots = [_tree_snapshot(member) for member in members]
            if any(snapshot is None for snapshot in snapshots):
                protected.append({"path": str(record), "reason": "unreadable ACPX session unit"})
                continue
            fallback_recorded_at = max(snapshot[1] for snapshot in snapshots if snapshot is not None)
            group_recorded_at = _recorded_at_from_payload(payload, fallback_recorded_at)
            candidates.extend((member, identity, None, record, group_recorded_at) for member in members)
    elif source.layout == "recursive_files":
        for path, is_link in _iter_regular_files_nofollow(source.root):
            if is_link:
                protected.append({"path": str(path), "reason": "symlink"})
                continue
            candidates.append((path, None, None, None, None))
    else:
        registry = source.root.parent / "active_sessions.json"
        active = _active_session_ids(registry)
        if active is None:
            protected.append({"path": str(source.root), "reason": "active session registry unavailable"})
            return grouped, protected
        for workspace in source.root.iterdir():
            if workspace.name.startswith(".") or _is_symlink(workspace) or not _is_dir_nofollow(workspace):
                continue
            for session in workspace.iterdir():
                if _is_symlink(session) or not _is_dir_nofollow(session):
                    continue
                if not _SESSION_ID_RE.fullmatch(session.name):
                    continue
                if session.name in active:
                    protected.append({"path": str(session), "reason": "active session"})
                    continue
                candidates.append((session, session.name, registry, None, None))

    for path, identity, registry, closure_record, group_recorded_at in candidates:
        if path.name in PROTECTED_NAMES or path.suffix in PROTECTED_SUFFIXES or _is_symlink(path):
            continue
        unit = _archive_unit(path, source=source, identity=identity, active_registry=registry)
        if unit is None:
            protected.append({"path": str(path), "reason": "unreadable or contains symlink"})
            continue
        if group_recorded_at is not None:
            unit = ArchiveUnit(
                path=unit.path,
                arcname=unit.arcname,
                recorded_at=group_recorded_at,
                size=unit.size,
                identity=unit.identity,
                fingerprint=unit.fingerprint,
                guard_root=unit.guard_root,
                active_registry=unit.active_registry,
                closure_record=closure_record,
            )
        if unit.recorded_at.timestamp() >= cutoff_ts:
            continue
        grouped[unit.recorded_at.strftime("%Y-%m")].append(unit)
    return grouped, protected


def _archive_variant(destination: Path, index: int) -> Path:
    """Retourne le nom mensuel canonique, puis ses variantes numérotées."""

    if index == 0:
        return destination
    suffix = ".tar.zst"
    base = destination.name[: -len(suffix)] if destination.name.endswith(suffix) else destination.name
    return destination.with_name(f"{base}.{index}{suffix}")


def _next_available_archive(destination: Path) -> Path:
    """Premier nom libre observé, utile au rapport dry-run."""

    index = 0
    while True:
        candidate = _archive_variant(destination, index)
        if not candidate.exists():
            return candidate
        index += 1


def _publish_archive(staged: Path, destination: Path) -> Path:
    """Publie ``staged`` sans jamais remplacer une archive existante.

    Le hard-link est une création atomique et exclusive. Comme le temporaire est
    créé dans le répertoire de destination, les deux chemins sont sur le même
    système de fichiers. Une exécution concurrente qui prend le nom attendu fait
    simplement essayer la variante numérotée suivante.
    """

    index = 0
    while True:
        candidate = _archive_variant(destination, index)
        try:
            os.link(staged, candidate)
        except FileExistsError:
            index += 1
            continue
        return candidate


def _coerce_archive_unit(value: Path | ArchiveUnit) -> ArchiveUnit:
    if isinstance(value, ArchiveUnit):
        return value
    snapshot = _tree_snapshot(value)
    if snapshot is None:
        raise OSError(f"archive source is unreadable: {value}")
    size, recorded_at, fingerprint = snapshot
    return ArchiveUnit(
        path=value,
        arcname=value.name,
        recorded_at=recorded_at,
        size=size,
        fingerprint=fingerprint,
        guard_root=value.parent,
    )


def _record_is_closed(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("closed") is True


def _unit_still_stable(unit: ArchiveUnit, *, active_ids: set[str] | None = None) -> bool:
    snapshot = _tree_snapshot(unit.path)
    if snapshot is None or (unit.fingerprint is not None and snapshot[2] != unit.fingerprint):
        return False
    if unit.closure_record is not None and not _record_is_closed(unit.closure_record):
        return False
    if unit.active_registry is not None:
        active = active_ids if active_ids is not None else _active_session_ids(unit.active_registry)
        if active is None or (unit.identity is not None and unit.identity in active):
            return False
    return True


@contextmanager
def _exclusive_flock(path: Path):
    fd = os.open(str(path), os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _parse_acpx_stream_lock_pid(raw: bytes) -> int | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _acpx_stream_lock_payload() -> bytes:
    return (json.dumps({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}) + "\n").encode(
        "utf-8"
    )


def _write_acpx_stream_lock(fd: int) -> None:
    payload = _acpx_stream_lock_payload()
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.fsync(fd)


def _recover_stale_acpx_stream_lock(lock_path: Path) -> None:
    """Unlink a crash-leftover O_EXCL lock whose recorded owner is proven dead.

    Invariants: a live pid, an opaque/unparseable payload, a symlink, or a held
    flock stays fail-closed. ACPX itself uses O_EXCL presence without flock, so
    flock success is not ownership. Steal only after flock (no live retention
    owner) AND a recorded pid that ``os.kill(..., 0)`` proves dead.
    """

    if _is_symlink(lock_path) or not _is_file_nofollow(lock_path):
        raise BlockingIOError("ACPX stream lock is held")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BlockingIOError("ACPX stream lock is held") from exc
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        pid = _parse_acpx_stream_lock_pid(raw)
        owner_alive = None if pid is None else _pid_is_alive(pid)
        if not acpx_stream_lock_is_recoverable(pid=pid, owner_alive=owner_alive):
            raise BlockingIOError("ACPX stream lock is held")
        os.unlink(lock_path)
    finally:
        os.close(fd)


@contextmanager
def _exclusive_acpx_stream_lock(lock_path: Path):
    """Acquire the ACPX session stream lock, recovering a stale dead-pid O_EXCL file."""

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except FileExistsError:
        _recover_stale_acpx_stream_lock(lock_path)
        try:
            fd = os.open(str(lock_path), flags, 0o600)
        except FileExistsError as exc:
            raise BlockingIOError("ACPX stream lock is held") from exc
    try:
        _write_acpx_stream_lock(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BlockingIOError("ACPX stream lock is held") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def _quarantine_root(guard_root: Path) -> Path:
    return quarantine_root(guard_root)


def _live_path(unit: ArchiveUnit) -> Path:
    if unit.guard_root is None:
        return unit.path
    quarantine = _quarantine_root(unit.guard_root)
    try:
        return unit.guard_root / unit.path.relative_to(quarantine)
    except ValueError:
        return unit.path


def _quarantine_unit(unit: ArchiveUnit) -> ArchiveUnit:
    if unit.guard_root is None:
        raise RuntimeError("refusing to quarantine without a guarded root")
    if _is_symlink(unit.path):
        raise RuntimeError(f"refusing to quarantine a symlink: {unit.path}")
    if not _confined_nofollow(unit.path, unit.guard_root):
        raise RuntimeError(f"refusing archive quarantine outside guarded root: {unit.path}")
    destination = planned_quarantine_path(unit.path, unit.guard_root)
    _ensure_confined_parents(destination, unit.guard_root, create=True)
    quarantine = _quarantine_root(unit.guard_root)
    os.chmod(quarantine, 0o700)
    if not _confined_nofollow(unit.path, unit.guard_root):
        raise RuntimeError(f"refusing archive quarantine outside guarded root: {unit.path}")
    if not _confined_nofollow(destination.parent, unit.guard_root):
        raise RuntimeError(f"refusing archive quarantine outside guarded root: {destination}")
    if _path_exists_nofollow(destination):
        raise RuntimeError(f"quarantine destination already exists: {destination}")
    os.rename(unit.path, destination)
    return replace(unit, path=destination)


def _ensure_confined_directory(path: Path, *, create: bool = False, parent: Path | None = None) -> None:
    if parent is None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise RuntimeError(f"missing directory: {path}") from None
    else:
        try:
            fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise RuntimeError(f"refusing symlink ancestor: {parent}") from exc
        try:
            try:
                st = os.lstat(path.name, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise RuntimeError(f"missing directory: {path}") from None
                try:
                    os.mkdir(path.name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    st = os.lstat(path.name, dir_fd=fd)
                except FileNotFoundError:
                    raise RuntimeError(f"missing directory: {path}") from None
        finally:
            os.close(fd)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink ancestor: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"conflicting parent is not a directory: {path}")


def _ensure_confined_parents(path: Path, guard_root: Path, *, create: bool = False) -> None:
    if not is_lexically_confined(path, guard_root):
        raise RuntimeError(f"path escapes guard root: {path}")
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    root = Path(os.path.normpath(os.path.abspath(guard_root)))
    _ensure_confined_directory(root)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"path escapes guard root: {path}") from exc
    current = root
    for part in relative.parts[:-1]:
        if part in {"", ".", ".."}:
            raise RuntimeError(f"unsafe path component: {part}")
        child = current / part
        _ensure_confined_directory(child, create=create, parent=current)
        current = child


def _restore_quarantined(units: list[ArchiveUnit]) -> None:
    for unit in reversed(units):
        if unit.guard_root is None:
            raise RuntimeError("refusing to restore without a guarded root")
        if unit.fingerprint is None:
            raise RuntimeError("refusing to restore without a fingerprint")
        original = _live_path(unit)
        if not is_lexically_confined(original, unit.guard_root):
            raise RuntimeError(f"live path escapes guard root: {original}")
        if _is_symlink(unit.path) or not _path_exists_nofollow(unit.path):
            raise RuntimeError(f"quarantine source missing or is a symlink: {unit.path}")
        if not _confined_nofollow(unit.path, unit.guard_root):
            raise RuntimeError(f"quarantine source is not confined: {unit.path}")
        if _path_exists_nofollow(original) or _is_symlink(original):
            raise RuntimeError(f"live destination already exists: {original}")
        snapshot = _tree_snapshot(unit.path)
        if snapshot is None or snapshot[2] != unit.fingerprint:
            raise RuntimeError("quarantine fingerprint does not match the durable intent")
        _ensure_confined_parents(original, unit.guard_root)
        os.rename(unit.path, original)
        if _path_exists_nofollow(unit.path):
            raise RuntimeError(f"quarantine still exists after restore: {unit.path}")
        if _is_symlink(original) or not _path_exists_nofollow(original):
            raise RuntimeError(f"live destination missing after restore: {original}")
        live_snapshot = _tree_snapshot(original)
        if live_snapshot is None or live_snapshot[2] != unit.fingerprint:
            raise RuntimeError("live fingerprint does not match the durable intent")
        _fsync_dir(unit.path.parent)
        _fsync_dir(original.parent)


def _retain_group(group: list[ArchiveUnit], reason: str) -> list[dict[str, str]]:
    return [{"path": str(unit.path), "reason": reason} for unit in group]


def _quarantine_units(units: list[ArchiveUnit]) -> tuple[list[ArchiveUnit], list[dict[str, str]]]:
    retained: list[dict[str, str]] = []
    quarantined: list[ArchiveUnit] = []
    grok_groups: dict[Path, list[ArchiveUnit]] = defaultdict(list)
    acpx_groups: dict[tuple[str, Path], list[ArchiveUnit]] = defaultdict(list)
    standalone: list[ArchiveUnit] = []
    for unit in units:
        if unit.active_registry is not None:
            grok_groups[unit.active_registry].append(unit)
        elif unit.closure_record is not None and unit.identity is not None and unit.guard_root is not None:
            acpx_groups[(unit.identity, unit.guard_root)].append(unit)
        else:
            standalone.append(unit)

    try:
        for registry, group in grok_groups.items():
            lock_path = registry.with_name("active_sessions.lock")
            try:
                with _exclusive_flock(lock_path):
                    active = _active_session_ids_unlocked(registry)
                    if active is None:
                        retained.extend(_retain_group(group, "active session registry unavailable"))
                        continue
                    moved = []
                    try:
                        for unit in group:
                            if not _unit_still_stable(unit, active_ids=active):
                                retained.append({"path": str(unit.path), "reason": "source changed or became active"})
                                continue
                            moved.append(_quarantine_unit(unit))
                    except BaseException:
                        _restore_quarantined(moved)
                        raise
                    quarantined.extend(moved)
            except BlockingIOError:
                retained.extend(_retain_group(group, "lifecycle lock unavailable"))
            except OSError as exc:
                retained.extend(_retain_group(group, f"lifecycle cannot be proven:{type(exc).__name__}"))

        for (identity, guard_root), group in acpx_groups.items():
            lock_path = guard_root / f"{identity}.stream.lock"
            try:
                with _exclusive_acpx_stream_lock(lock_path):
                    if any(not _unit_still_stable(unit) for unit in group):
                        retained.extend(_retain_group(group, "source changed or became active"))
                        continue
                    moved = []
                    try:
                        for unit in group:
                            moved.append(_quarantine_unit(unit))
                    except BaseException:
                        _restore_quarantined(moved)
                        raise
                    quarantined.extend(moved)
            except BlockingIOError:
                retained.extend(_retain_group(group, "lifecycle lock unavailable"))
            except OSError as exc:
                retained.extend(_retain_group(group, f"lifecycle cannot be proven:{type(exc).__name__}"))

        for unit in standalone:
            if not _unit_still_stable(unit):
                retained.append({"path": str(unit.path), "reason": "source changed or became active"})
                continue
            try:
                quarantined.append(_quarantine_unit(unit))
            except OSError as exc:
                retained.append({"path": str(unit.path), "reason": f"lifecycle cannot be proven:{type(exc).__name__}"})
        return quarantined, retained
    except BaseException:
        _restore_quarantined(quarantined)
        raise


def _rmtree_child(parent_fd: int, name: str) -> None:
    """Delete ``name`` inside a held parent directory fd. Never follows a symlink."""

    if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise RuntimeError(f"unsafe directory entry: {name}")
    try:
        st = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = _open_directory_nofollow(name, dir_fd=parent_fd)
    try:
        fst = os.fstat(child_fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise RuntimeError(f"directory inode changed during deletion: {name}")
        for entry in os.listdir(child_fd):
            _rmtree_child(child_fd, entry)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_confined(path: Path, guard_root: Path) -> None:
    """Delete ``path`` using openat/unlinkat/rmdirat under ``guard_root``."""

    if _is_symlink(path):
        raise RuntimeError(f"refusing to delete a symlink: {path}")
    if not is_lexically_confined(path, guard_root) or not _confined_nofollow(path, guard_root):
        raise RuntimeError(f"refusing archive deletion outside guarded root: {path}")
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    root = Path(os.path.normpath(os.path.abspath(guard_root)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"refusing archive deletion outside guarded root: {path}") from exc
    parts = [part for part in relative.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise RuntimeError(f"refusing to delete the guarded root: {path}")
    root_fd = _open_directory_nofollow(root)
    opened: list[int] = []
    try:
        parent_fd = root_fd
        for part in parts[:-1]:
            child_fd = _open_directory_nofollow(part, dir_fd=parent_fd)
            opened.append(child_fd)
            parent_fd = child_fd
        _rmtree_child(parent_fd, parts[-1])
    finally:
        for held in reversed(opened):
            os.close(held)
        os.close(root_fd)


def _remove_unit(unit: ArchiveUnit) -> None:
    path = unit.path
    live = _live_path(unit)
    if unit.guard_root is None:
        raise RuntimeError("refusing to delete without a guarded root")
    if _is_symlink(path):
        raise RuntimeError(f"refusing to delete a symlink: {path}")
    _remove_confined(path, unit.guard_root)
    guard = unit.guard_root
    seen: set[Path] = set()
    for start in (path.parent, live.parent):
        parent = start
        while parent not in seen and parent != guard and is_lexically_confined(parent, guard):
            seen.add(parent)
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return f"sha256:{digest.hexdigest()}"


def _assert_safe_arcname(name: str) -> None:
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"archive tar verification failed: unsafe member {name}")


def _tar_member_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if tarinfo.issym() or tarinfo.islnk():
        return None
    _assert_safe_arcname(tarinfo.name)
    return tarinfo


def _verify_published_archive(published: Path, units: list[ArchiveUnit], *, tmpdir: Path) -> None:
    zstd = resolve_zstd()
    try:
        subprocess.run([zstd, "-t", "-q", str(published)], check=True)
        tar_path = tmpdir / "verified.tar"
        subprocess.run([zstd, "-d", "-q", "-o", str(tar_path), str(published)], check=True)
        with tarfile.open(tar_path, "r") as verifier:
            names = verifier.getnames()
            for name in names:
                _assert_safe_arcname(name)
            expected = {unit.arcname for unit in units}
            if "_archive/manifest.json" not in names:
                raise RuntimeError("archive tar verification failed: expected members are missing")
            manifest_file = verifier.extractfile("_archive/manifest.json")
            if manifest_file is None:
                raise RuntimeError("archive tar verification failed: expected members are missing")
            manifest = json.loads(manifest_file.read().decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != "agent_storage_archive.v1":
                raise RuntimeError("archive tar verification failed: invalid manifest")
            members = manifest.get("members")
            if not isinstance(members, list):
                raise RuntimeError("archive tar verification failed: invalid manifest")
            manifest_arcnames = {
                item["arcname"] for item in members if isinstance(item, dict) and isinstance(item.get("arcname"), str)
            }
            if manifest_arcnames != expected:
                raise RuntimeError("archive tar verification failed: manifest members mismatch")
            for unit in units:
                if not any(name == unit.arcname or name.startswith(f"{unit.arcname}/") for name in names):
                    raise RuntimeError("archive tar verification failed: expected members are missing")
                member = verifier.getmember(unit.arcname)
                if member.isfile() and member.size != unit.size:
                    raise RuntimeError("archive tar verification failed: member size mismatch")
    except (tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"archive tar verification failed: {exc}") from exc


def _intent_source_id(units: list[ArchiveUnit], source_id: str | None) -> str | None:
    if source_id is not None:
        return source_id
    if not any(unit.identity for unit in units):
        return None
    prefixes = {PurePosixPath(unit.arcname).parts[0] for unit in units if unit.arcname}
    if len(prefixes) != 1:
        return None
    candidate = next(iter(prefixes))
    try:
        provider_for_source_id(candidate)
    except ValueError:
        return None
    return candidate


def _drop_intent(intent: QuarantineTransactionIntent | None) -> None:
    if intent is None:
        return
    drop_quarantine_intent(intent)
    _cleanup_quarantine_tree(intent)


def archive_month(
    paths: list[Path | ArchiveUnit],
    destination: Path,
    *,
    apply: bool,
    commit_identities: Callable[[frozenset[str]], None] | None = None,
    source_id: str | None = None,
) -> dict:
    """Écrit ``destination`` (tar.zst) puis supprime les sources. Fail-closed.

    Un intent fsyncé est persisté avant le premier rename live→quarantaine.
    L'archive publiée est fsyncée et relue (zstd + tar + manifeste) ; son sha256
    canonique est enregistré dans l'intent. À partir de cet intent durable, le
    replay possède la quarantaine : un crash ne restaure plus et ne drop plus.
    """

    units = [_coerce_archive_unit(path) for path in paths]
    raw_size = sum(unit.size for unit in units)
    result = {
        "files": len(units),
        "logical_units": len({unit.identity or unit.arcname for unit in units}),
        "raw_size": raw_size,
        "raw_size_human": _human(raw_size),
        "archive": str(_next_available_archive(destination)),
        "applied": False,
        "removed": 0,
        "retained": [],
    }
    if not apply:
        return result

    zstd = resolve_zstd()
    _ensure_real_archive_root(destination.parent)
    quarantined: list[ArchiveUnit] = []
    published: Path | None = None
    intent: QuarantineTransactionIntent | None = None
    retained: list[dict[str, str]] = []
    try:
        if not units:
            return result
        _assert_real_directory(destination.parent, what="archive root")
        intent = QuarantineTransactionIntent.from_archive_units(
            units,
            transaction_id=uuid.uuid4().hex,
            source_id=_intent_source_id(units, source_id),
            destination=destination,
        )
        save_quarantine_intent(intent)
        quarantined, retained = _quarantine_units(units)
        result["retained"] = retained
        if not quarantined:
            _drop_intent(intent)
            intent = None
            return result
        intent = intent.with_units(tuple(QuarantineUnitIntent.from_archive_unit(unit) for unit in quarantined))
        save_quarantine_intent(intent)
        _assert_real_directory(destination.parent, what="archive root")
        with tempfile.TemporaryDirectory(dir=str(destination.parent)) as tmpdir:
            tar_path = Path(tmpdir) / "sessions.tar"
            with tarfile.open(tar_path, "w") as tar:
                for unit in quarantined:
                    tar.add(unit.path, arcname=unit.arcname, recursive=True, filter=_tar_member_filter)
                manifest_payload = {
                    "schema_version": "agent_storage_archive.v1",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "members": [
                        {
                            "arcname": unit.arcname,
                            "identity": unit.identity,
                            "recorded_at": unit.recorded_at.isoformat(),
                            "size": unit.size,
                            "fingerprint": unit.fingerprint,
                        }
                        for unit in quarantined
                    ],
                }
                manifest_bytes = json.dumps(
                    manifest_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                manifest_info = tarfile.TarInfo("_archive/manifest.json")
                manifest_info.size = len(manifest_bytes)
                manifest_info.mtime = int(datetime.now(timezone.utc).timestamp())
                tar.addfile(manifest_info, io.BytesIO(manifest_bytes))
            staged = Path(tmpdir) / destination.name
            subprocess.run(
                [zstd, f"-{ZSTD_LEVEL}", "-T0", "-q", "-o", str(staged), str(tar_path)],
                check=True,
            )
            staged.chmod(0o600)
            _fsync_file(staged)
            _assert_real_directory(destination.parent, what="archive root")
            published = _publish_archive(staged, destination)
            _fsync_file(published)
            _fsync_dir(published.parent)
            digest = _sha256_file(published)
            _verify_published_archive(published, quarantined, tmpdir=Path(tmpdir))
            if _sha256_file(published) != digest:
                raise RuntimeError("published archive changed during verification")
            intent = intent.with_verified_archive(published, digest)
            save_quarantine_intent(intent)
    except BaseException:
        if published is not None and (intent is None or not intent.archive_verified):
            published.unlink(missing_ok=True)
        _restore_quarantined(quarantined)
        _drop_intent(intent)
        raise

    # Verified intent is durable: replay owns quarantine. Do not restore or drop
    # on a later crash; journal absent restores, journal + verified archive finalizes.
    removed_identities = {unit.identity for unit in quarantined if unit.identity is not None}
    if commit_identities is not None and removed_identities:
        commit_identities(frozenset(removed_identities))

    removed: list[str] = []
    removed_size = 0
    for unit in quarantined:
        live = _live_path(unit)
        _remove_unit(unit)
        removed.append(str(live))
        removed_size += unit.size

    _drop_intent(intent)
    packed = published.stat().st_size
    result.update(
        applied=True,
        archive=str(published),
        archive_sha256=_sha256_file(published),
        archive_size=packed,
        archive_size_human=_human(packed),
        ratio=round(raw_size / packed, 1) if packed else None,
        removed=len(removed),
        removed_size=removed_size,
        removed_size_human=_human(removed_size),
        removed_identities=sorted(removed_identities),
        retained=retained,
    )
    return result


def _extract_llm_usage_before_purge() -> dict:
    """Extract durable usage before retention; callers fail closed on errors."""

    try:
        from scripts.llm_cost import extract_and_append

        return extract_and_append(repo_root=REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - converted to an explicit retention gate
        return {"error": str(exc), "appended": 0}


def _vacuum_sqlite(path: Path, *, timeout: float = 5.0, file_fd: int | None = None) -> None:
    if file_fd is not None:
        with sqlite3.connect(_sqlite_dev_fd_path(file_fd), isolation_level=None, timeout=timeout) as conn:
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("VACUUM")
        return
    with sqlite3.connect(path, isolation_level=None, timeout=timeout) as conn:
        conn.execute("VACUUM")


def purge_codex_logs(home: Path, *, apply: bool) -> list[dict]:
    """Vide les tables de logs des bases `logs_*.sqlite` puis compacte."""

    out: list[dict] = []
    try:
        os.lstat(home)
    except FileNotFoundError:
        return out
    except OSError as exc:
        return [
            {
                "path": str(home),
                "applied": False,
                "delete_committed": False,
                "vacuumed": False,
                "error": f"{type(exc).__name__}:{exc}",
            }
        ]
    if _is_symlink(home) or not _is_dir_nofollow(home):
        return [
            {
                "path": str(home),
                "applied": False,
                "delete_committed": False,
                "vacuumed": False,
                "error": "refusing to DELETE/VACUUM a symlinked SQLite target",
            }
        ]
    for db_path in sorted(home.glob("logs_*.sqlite")):
        try:
            size = os.lstat(db_path).st_size
        except OSError:
            size = 0
        entry = {
            "path": str(db_path),
            "size": size,
            "size_human": _human(size),
            "applied": False,
            "delete_committed": False,
            "vacuumed": False,
        }
        if _is_symlink(db_path) or not _is_file_nofollow(db_path) or not _confined_nofollow(db_path, home):
            entry["error"] = "refusing to DELETE/VACUUM a symlinked SQLite target"
            out.append(entry)
            continue
        try:
            with sqlite3.connect(db_path) as conn:
                entry["rows"] = conn.execute("SELECT count(*) FROM logs").fetchone()[0]
                if apply:
                    conn.execute("DELETE FROM logs")
                    conn.commit()
                    entry["delete_committed"] = True
            if apply:
                try:
                    _vacuum_sqlite(db_path)
                    entry.update(
                        applied=True,
                        vacuumed=True,
                        size_after=os.lstat(db_path).st_size,
                        size_after_human=_human(os.lstat(db_path).st_size),
                    )
                except sqlite3.Error as exc:
                    entry.update(
                        applied=True,
                        error=f"VACUUM failed after DELETE committed: {exc}",
                        size_after=os.lstat(db_path).st_size,
                        size_after_human=_human(os.lstat(db_path).st_size),
                    )
        except sqlite3.Error as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return out


def purge_generated_files(source: PurgeSource, cutoff: datetime, *, apply: bool) -> dict:
    """Remove only allowlisted, regenerable files older than the retention window."""

    candidates: list[Path] = []
    protected: list[dict[str, str]] = []
    unusable = None
    try:
        os.lstat(source.root)
    except FileNotFoundError:
        unusable = "missing"
    except OSError as exc:
        unusable = f"unreadable:{exc}"
    if unusable is None and (_is_symlink(source.root) or not _is_dir_nofollow(source.root)):
        unusable = "declared root is a symlink"
    if (
        unusable is None
        and source.authority_root is not None
        and not _confined_nofollow(source.root, source.authority_root)
    ):
        unusable = "declared root is not confined (symlink in authority path)"
    if unusable is not None:
        return {
            "source_id": source.source_id,
            "root": str(source.root),
            "files": 0,
            "size": 0,
            "size_human": _human(0),
            "removed": 0,
            "applied": False,
            "protected": [],
            "reason": unusable,
            "error": None if unusable == "missing" else unusable,
        }
    for path, is_link in _iter_regular_files_nofollow(source.root):
        if is_link or path.name in PROTECTED_NAMES or path.suffix in PROTECTED_SUFFIXES:
            protected.append({"path": str(path), "reason": "protected generated entry"})
            continue
        try:
            if os.lstat(path).st_mtime < cutoff.timestamp():
                candidates.append(path)
        except OSError:
            protected.append({"path": str(path), "reason": "unreadable generated entry"})
    size = 0
    for path in candidates:
        try:
            size += os.lstat(path).st_size
        except OSError:
            protected.append({"path": str(path), "reason": "unreadable generated entry"})
    removed = 0
    if apply:
        for path in candidates:
            if _is_symlink(path) or not _confined_nofollow(path, source.root):
                protected.append({"path": str(path), "reason": "outside guarded generated root"})
                continue
            path.unlink(missing_ok=True)
            removed += 1
        stack = [source.root]
        directories: list[Path] = []
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as scanned:
                    children = list(scanned)
            except OSError:
                continue
            for entry in children:
                child = Path(entry.path)
                if entry.is_symlink() or entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(child)
                    stack.append(child)
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "source_id": source.source_id,
        "root": str(source.root),
        "files": len(candidates),
        "size": size,
        "size_human": _human(size),
        "removed": removed,
        "applied": apply,
        "protected": protected,
    }


def _rewrite_prompt_history(path: Path, identities: set[str], *, apply: bool) -> dict:
    """Drop redundant prompt-history rows only after their session bundle is archived."""

    if _is_symlink(path) or not _is_file_nofollow(path):
        raise RuntimeError("prompt history is a symlink")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    temporary: Path | None = None
    output = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("prompt history lock unavailable") from exc
        before = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as source:
            original = source.read()
        matched_lines = 0
        matched_bytes = 0
        kept_bytes = 0
        kept: list[bytes] = []
        for line in original.splitlines(keepends=True):
            remove = False
            try:
                payload = json.loads(line)
                remove = isinstance(payload, dict) and str(payload.get("session_id") or "") in identities
            except json.JSONDecodeError:
                pass
            if remove:
                matched_lines += 1
                matched_bytes += len(line)
            else:
                kept_bytes += len(line)
                kept.append(line)
        if apply:
            output = tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False)
            temporary = Path(output.name)
            output.write(b"".join(kept))
            output.flush()
            os.fsync(output.fileno())
            output.close()
            output = None
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or path.read_bytes() != original:
                raise RuntimeError("prompt history changed during reconciliation")
            os.chmod(temporary, before.st_mode & 0o777)
            if path.read_bytes() != original:
                raise RuntimeError("prompt history changed during reconciliation")
            os.replace(temporary, path)
            temporary = None
        return {
            "path": str(path),
            "matched_lines": matched_lines,
            "matched_bytes": matched_bytes,
            "matched_bytes_human": _human(matched_bytes),
            "kept_bytes": kept_bytes,
            "applied": apply,
        }
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        if output is not None:
            output.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _require_sqlite_fd_binding() -> None:
    if not os.path.exists("/dev/fd"):
        raise RuntimeError("sqlite cannot bind a no-follow fd: /dev/fd is unavailable")


def _sqlite_dev_fd_path(file_fd: int) -> str:
    _require_sqlite_fd_binding()
    return f"/dev/fd/{file_fd}"


def _sqlite_connect_held(file_fd: int, **kwargs):
    try:
        return sqlite3.connect(_sqlite_dev_fd_path(file_fd), **kwargs)
    except sqlite3.Error as exc:
        raise RuntimeError(f"sqlite cannot bind held fd: {exc}") from exc


def _sqlite_file_bytes_at(dir_fd: int, name: str) -> int:
    total = 0
    for candidate in (name, f"{name}-wal", f"{name}-shm"):
        try:
            st = os.lstat(candidate, dir_fd=dir_fd)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        total += st.st_size
    return total


def _read_json_at(dir_fd: int, name: str) -> object:
    fd = _open_regular_nofollow(name, dir_fd=dir_fd, flags=os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _write_json_atomically_at(dir_fd: int, name: str, payload: dict[str, object]) -> None:
    tmp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp_name, _file_fd_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.fsync(dir_fd)


def _load_session_docs_vacuum_journal_at(dir_fd: int, sqlite_name: str) -> SessionDocsVacuumJournal | None:
    name = session_docs_vacuum_journal_path(Path(sqlite_name)).name
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError("session_docs vacuum journal is a symlink")
    if not stat.S_ISREG(st.st_mode):
        return None
    payload = _read_json_at(dir_fd, name)
    journal = SessionDocsVacuumJournal.from_payload(payload)
    if journal.sqlite_name != sqlite_name:
        raise RuntimeError("session_docs vacuum journal sqlite_name mismatch")
    return journal


def _save_session_docs_vacuum_journal_at(dir_fd: int, sqlite_name: str, identities: set[str]) -> None:
    journal = SessionDocsVacuumJournal(
        sqlite_name=sqlite_name,
        identities=tuple(identities),
        created_at=datetime.now(timezone.utc),
    )
    _write_json_atomically_at(dir_fd, session_docs_vacuum_journal_path(Path(sqlite_name)).name, journal.to_payload())


def _drop_session_docs_vacuum_journal_at(dir_fd: int, sqlite_name: str) -> None:
    name = session_docs_vacuum_journal_path(Path(sqlite_name)).name
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError("session_docs vacuum journal is a symlink")
    if not stat.S_ISREG(st.st_mode):
        return
    os.unlink(name, dir_fd=dir_fd)
    os.fsync(dir_fd)


def _session_search_error(path: Path, exc: BaseException, *, matched: int = 0, delete_committed: bool = False) -> dict:
    return {
        "path": str(path),
        "matched_rows": matched,
        "applied": False,
        "delete_committed": delete_committed,
        "vacuumed": False,
        "error": str(exc),
    }


def _reconcile_session_search_held(
    path: Path,
    identities: set[str],
    *,
    apply: bool,
    dir_fd: int,
    file_fd: int,
    sqlite_name: str,
) -> dict:
    try:
        journal = _load_session_docs_vacuum_journal_at(dir_fd, sqlite_name)
    except (OSError, RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        error = (
            str(exc) if "symlink" in str(exc) else f"session_docs vacuum journal unreadable:{type(exc).__name__}:{exc}"
        )
        return {
            "path": str(path),
            "matched_rows": 0,
            "applied": False,
            "delete_committed": False,
            "vacuumed": False,
            "error": error,
        }
    pending = set(identities)
    if journal is not None:
        pending.update(journal.identities)
    before = _sqlite_file_bytes_at(dir_fd, sqlite_name)
    matched = 0
    delete_committed = False
    vacuumed = False
    try:
        with _sqlite_connect_held(file_fd, timeout=5) as connection:
            if apply:
                connection.execute("PRAGMA journal_mode=MEMORY")
                connection.execute("PRAGMA temp_store=MEMORY")
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session_docs" not in tables:
                return {"path": str(path), "matched_rows": 0, "applied": False, "reason": "unknown_schema"}
            ordered = sorted(pending)
            for start in range(0, len(ordered), 400):
                batch = ordered[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                matched += int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM session_docs WHERE session_id IN ({placeholders})",
                        batch,
                    ).fetchone()[0]
                )
            needs_compact = bool(matched or journal is not None)
            if apply and needs_compact:
                if journal is None:
                    _save_session_docs_vacuum_journal_at(dir_fd, sqlite_name, pending)
                for start in range(0, len(ordered), 400):
                    batch = ordered[start : start + 400]
                    placeholders = ",".join("?" for _ in batch)
                    connection.execute(f"DELETE FROM session_docs WHERE session_id IN ({placeholders})", batch)
                fts_definition = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_docs_fts'"
                ).fetchone()
                if fts_definition and "USING fts5" in str(fts_definition[0]):
                    # FTS5 conserve sinon des segments/tombstones volumineux apres
                    # les DELETE. L'index est une projection regenerable de
                    # session_docs : le reconstruire est la compaction canonique.
                    connection.execute("INSERT INTO session_docs_fts(session_docs_fts) VALUES('rebuild')")
                    connection.execute(
                        "INSERT INTO session_docs_fts(session_docs_fts, rank) VALUES('integrity-check', 1)"
                    )
                connection.commit()
                delete_committed = True
        if apply and (matched or journal is not None):
            try:
                _vacuum_sqlite(path, timeout=5, file_fd=file_fd)
                with _sqlite_connect_held(file_fd, isolation_level=None, timeout=5) as connection:
                    connection.execute("PRAGMA journal_mode=MEMORY")
                    connection.execute("PRAGMA temp_store=MEMORY")
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                vacuumed = True
                _drop_session_docs_vacuum_journal_at(dir_fd, sqlite_name)
            except sqlite3.Error as exc:
                after = _sqlite_file_bytes_at(dir_fd, sqlite_name)
                return {
                    "path": str(path),
                    "matched_rows": matched,
                    "size_before": before,
                    "size_after": after,
                    "reclaimed_bytes": max(0, before - after),
                    "applied": True,
                    "delete_committed": delete_committed,
                    "vacuumed": False,
                    "error": f"VACUUM failed after DELETE committed: {exc}",
                }
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return {
            "path": str(path),
            "matched_rows": matched,
            "applied": False,
            "delete_committed": delete_committed,
            "vacuumed": False,
            "error": str(exc),
        }
    after = _sqlite_file_bytes_at(dir_fd, sqlite_name)
    return {
        "path": str(path),
        "matched_rows": matched,
        "size_before": before,
        "size_after": after,
        "reclaimed_bytes": max(0, before - after),
        "applied": apply,
        "delete_committed": delete_committed,
        "vacuumed": vacuumed,
        "vacuum_replayed": journal is not None and vacuumed,
    }


def _reconcile_session_search(
    path: Path,
    identities: set[str],
    *,
    apply: bool,
    authority: Path | None = None,
) -> dict:
    missing = {"path": str(path), "matched_rows": 0, "applied": False, "reason": "missing"}
    root = authority if authority is not None else path.parent
    opened: list[int] = []
    try:
        if not is_lexically_confined(path, root):
            return _session_search_error(path, RuntimeError("sqlite path escapes authority"))
        candidate = Path(os.path.normpath(os.path.abspath(path)))
        auth = Path(os.path.normpath(os.path.abspath(root)))
        relative = candidate.relative_to(auth)
        parts = [part for part in relative.parts if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            return _session_search_error(path, RuntimeError("sqlite path escapes authority"))
        parent_fd = _open_directory_nofollow(auth)
        opened.append(parent_fd)
        fd = parent_fd
        for part in parts[:-1]:
            child_fd = _open_directory_nofollow(part, dir_fd=fd)
            opened.append(child_fd)
            fd = child_fd
        try:
            file_fd = _open_regular_nofollow(parts[-1], dir_fd=fd, flags=os.O_RDWR)
        except FileNotFoundError:
            return missing
        try:
            return _reconcile_session_search_held(
                path,
                identities,
                apply=apply,
                dir_fd=fd,
                file_fd=file_fd,
                sqlite_name=parts[-1],
            )
        finally:
            os.close(file_fd)
    except FileNotFoundError:
        return missing
    except (OSError, RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _session_search_error(path, exc)
    finally:
        for held in reversed(opened):
            os.close(held)


def _prompt_histories_from_sessions_fd(sessions_fd: int, sessions_root: Path) -> list[Path]:
    histories: list[Path] = []
    for name in os.listdir(sessions_fd):
        if name.startswith("."):
            continue
        try:
            st = os.lstat(name, dir_fd=sessions_fd)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            continue
        workspace_fd = _open_directory_nofollow(name, dir_fd=sessions_fd)
        try:
            try:
                hist_st = os.lstat("prompt_history.jsonl", dir_fd=workspace_fd)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(hist_st.st_mode) or not stat.S_ISREG(hist_st.st_mode):
                continue
            histories.append(sessions_root / name / "prompt_history.jsonl")
        finally:
            os.close(workspace_fd)
    return histories


def reconcile_grok_session_metadata(home: Path, identities: set[str], *, apply: bool) -> dict:
    """Reconcile redundant history and derived FTS rows for archived Grok sessions."""

    result: dict = {
        "home": str(home),
        "identities": len(identities),
        "prompt_histories": [],
        "session_search": None,
        "applied": apply,
    }
    if not identities:
        return result
    lock_path = home / "active_sessions.lock"
    registry = home / "active_sessions.json"
    if (
        not _is_file_nofollow(lock_path)
        or not _is_file_nofollow(registry)
        or _is_symlink(lock_path)
        or _is_symlink(registry)
        or _is_symlink(home)
    ):
        return {**result, "applied": False, "error": "active session registry unavailable"}
    lock_handle = None
    home_fd: int | None = None
    try:
        home_fd = _open_directory_nofollow(home)
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_NOFOLLOW)
        lock_handle = os.fdopen(lock_fd, "rb")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        active = _active_session_ids_unlocked(registry)
        if active is None:
            return {**result, "applied": False, "error": "active session registry unreadable"}
        overlap = sorted(active.intersection(identities))
        if overlap:
            return {**result, "applied": False, "error": "archived identity became active"}
        sessions_root = home / "sessions"
        try:
            sessions_st = os.lstat("sessions", dir_fd=home_fd)
        except FileNotFoundError:
            result["session_search"] = {
                "path": str(sessions_root / "session_search.sqlite"),
                "matched_rows": 0,
                "applied": False,
                "reason": "missing",
            }
            return result
        if stat.S_ISLNK(sessions_st.st_mode):
            return {**result, "applied": False, "error": "refusing symlink in directory chain: sessions"}
        sessions_fd = _open_directory_nofollow("sessions", dir_fd=home_fd)
        try:
            if not _held_dir_matches_path(sessions_root, sessions_fd):
                return {**result, "applied": False, "error": "refusing symlink in directory chain: sessions"}
            for path in sorted(_prompt_histories_from_sessions_fd(sessions_fd, sessions_root)):
                if not _held_dir_matches_path(sessions_root, sessions_fd):
                    return {**result, "applied": False, "error": "refusing symlink in directory chain: sessions"}
                result["prompt_histories"].append(_rewrite_prompt_history(path, identities, apply=apply))
            result["session_search"] = _reconcile_session_search(
                sessions_root / "session_search.sqlite",
                identities,
                apply=apply,
                authority=home,
            )
            search = result["session_search"]
            if search.get("error"):
                result["applied"] = False
                result["error"] = search["error"]
            return result
        finally:
            os.close(sessions_fd)
    except (BlockingIOError, OSError, sqlite3.Error, RuntimeError) as exc:
        return {**result, "applied": False, "error": f"{type(exc).__name__}:{exc}"}
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()
        if home_fd is not None:
            os.close(home_fd)


def _resolve_acpx() -> str | None:
    found = shutil.which("acpx")
    if found:
        return found
    return next((candidate for candidate in _ACPX_FALLBACK_PATHS if Path(candidate).is_file()), None)


def child_path_for_executable(executable: str, *, existing: str | None = None) -> str:
    """Build a PATH that lets shebang scripts find sibling interpreters.

    launchd's default PATH is ``/usr/bin:/bin``. ACPX is a ``#!/usr/bin/env node``
    script, so resolving the binary absolutely is not enough: the child still
    looks up ``node`` on PATH. Put the executable's directory first, then the
    inherited PATH, then ``os.defpath``.
    """

    inherited = os.environ.get("PATH", "") if existing is None else existing
    parts: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        if not raw:
            return
        normalized = os.path.normpath(raw)
        if normalized in seen:
            return
        seen.add(normalized)
        parts.append(normalized)

    binary = Path(executable)
    add(str(binary.parent))
    try:
        add(str(binary.resolve().parent))
    except OSError:
        pass
    for part in inherited.split(os.pathsep):
        add(part)
    for part in os.defpath.split(os.pathsep):
        add(part)
    return os.pathsep.join(parts)


def _child_env_for_executable(executable: str, *, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["PATH"] = child_path_for_executable(executable, existing=env.get("PATH", ""))
    return env


def _acpx_reconciliation_error(exc: BaseException) -> str:
    detail = f"{type(exc).__name__}:{exc}"
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str) and stderr.strip():
        detail = f"{detail} stderr={stderr.strip()}"
    return f"{detail}; index will self-heal on next ACPX read"


def _acpx_index_is_coherent(root: Path) -> tuple[bool, int]:
    index_path = root / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, 0
    if not isinstance(payload, dict) or payload.get("schema") != "acpx.session-index.v1":
        return False, 0
    files = payload.get("files")
    entries = payload.get("entries")
    if not isinstance(files, list) or not isinstance(entries, list):
        return False, 0
    actual = sorted(path.name for path in root.glob("*.json") if path.name != "index.json" and not _is_symlink(path))
    indexed = sorted(item for item in files if isinstance(item, str))
    entry_files = sorted(
        str(item.get("file")) for item in entries if isinstance(item, dict) and isinstance(item.get("file"), str)
    )
    coherent = indexed == actual and entry_files == actual
    return coherent, len(actual)


def reconcile_acpx_session_index(root: Path, identities: set[str], *, apply: bool) -> dict:
    """Ask ACPX itself to atomically rebuild its derived session index."""

    result = {
        "home": str(root.parent),
        "kind": "acpx_session_index",
        "identities": len(identities),
        "applied": False,
    }
    if not identities:
        return {**result, "reason": "no_archived_identity"}
    if not apply:
        return {**result, "reason": "dry_run"}
    binary = _resolve_acpx()
    if binary is None:
        return {**result, "error": "acpx executable unavailable; index will self-heal on next ACPX read"}
    try:
        subprocess.run(
            [binary, "--format", "json", "sessions", "list", "--local"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            env=_child_env_for_executable(binary),
        )
        coherent, records = _acpx_index_is_coherent(root)
        if not coherent:
            return {**result, "error": "ACPX index rebuild did not match surviving records"}
        return {**result, "applied": True, "records": records}
    except (OSError, subprocess.SubprocessError) as exc:
        return {**result, "error": _acpx_reconciliation_error(exc)}


def _ops_inventory(ops_root: Path, sources: tuple[ArchiveSource, ...]) -> dict:
    known_home_names = {source.source_id for source in sources if source.source_id != "acpx"}
    discovered_homes = (
        sorted(
            path.name
            for path in ops_root.iterdir()
            if _is_dir_nofollow(path) and not _is_symlink(path) and re.fullmatch(r".+-home(?:-.+)?", path.name)
        )
        if _is_dir_nofollow(ops_root) and not _is_symlink(ops_root)
        else []
    )
    unknown_homes = sorted(set(discovered_homes).difference(known_home_names))
    return {
        "root": str(ops_root),
        "discovered_homes": discovered_homes,
        "protected_unknown_homes": unknown_homes,
        "protected_operational_roots": [
            str(ops_root / "launchd"),
            str(ops_root / "model-presets"),
        ],
    }


def _archive_destination(source: ArchiveSource, month: str) -> Path:
    prefix = "acpx-sessions" if source.source_id == "acpx" else f"{source.source_id}-{source.category}"
    return ARCHIVE_ROOT / f"{prefix}-{month}.tar.zst"


def secure_archive_storage(root: Path, *, apply: bool) -> dict:
    """Keep transcript archives private, including files produced by older versions."""

    files = (
        sorted(path for path in root.glob("*.tar.zst") if _is_file_nofollow(path) and not _is_symlink(path))
        if _is_dir_nofollow(root) and not _is_symlink(root)
        else []
    )
    loose_files = [path for path in files if os.lstat(path).st_mode & 0o077]
    loose_root = _is_dir_nofollow(root) and not _is_symlink(root) and bool(os.lstat(root).st_mode & 0o077)
    if apply and _is_dir_nofollow(root) and not _is_symlink(root):
        os.chmod(root, 0o700)
        for path in files:
            os.chmod(path, 0o600)
    return {
        "root": str(root),
        "archive_files": len(files),
        "files_requiring_private_mode": len(loose_files),
        "root_requiring_private_mode": loose_root,
        "applied": apply,
    }


def _write_report(report: dict, *, output_format: str) -> None:
    if output_format == "jsonl":
        json.dump(report, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    else:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than",
        type=int,
        default=DEFAULT_OLDER_THAN_DAYS,
        help=f"âge minimum en jours des sessions à archiver (défaut {DEFAULT_OLDER_THAN_DAYS})",
    )
    parser.add_argument(
        "--only",
        choices=("sessions", "logs", "traces", "cache"),
        help="ne traiter qu'une classe (défaut : toutes les politiques déclarées)",
    )
    parser.add_argument(
        "--home",
        action="append",
        default=[],
        help="limiter à un home connu (répétable, basename uniquement)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="écrire réellement (sans ce drapeau : simulation)",
    )
    parser.add_argument(
        "--output-format",
        choices=("pretty", "jsonl"),
        default="pretty",
        help="format du rapport (jsonl convient aux logs append-only planifiés)",
    )
    args = parser.parse_args(argv)

    if args.older_than < 1:
        parser.error("--older-than doit valoir au moins 1 jour")

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    sources, purge_sources = agent_storage_policy(CODEX_HOME.parent)
    known_homes = {source.source_id for source in sources} | {source.source_id for source in purge_sources}
    requested_homes = set(args.home)
    unknown_requested = sorted(requested_homes.difference(known_homes))
    if unknown_requested:
        parser.error(f"home inconnu: {', '.join(unknown_requested)}")

    report: dict = {
        "schema_version": "agent_storage_retention.v2",
        "mode": "apply" if args.apply else "dry-run",
        "cutoff": cutoff.isoformat(),
        "sessions": [],
        "logs": [],
        "archives": [],
        "purges": [],
        "metadata_reconciliation": [],
        "protected": [],
        "inventory": _ops_inventory(CODEX_HOME.parent, sources),
        "status": "ok",
    }

    try:
        if args.apply:
            with _hold_retention_apply_lock() as lock_state:
                report["retention_lock"] = lock_state
                return _run_retention(
                    args,
                    report,
                    cutoff=cutoff,
                    sources=sources,
                    purge_sources=purge_sources,
                    requested_homes=requested_homes,
                )
        report["retention_lock"] = _probe_retention_lock()
        return _run_retention(
            args,
            report,
            cutoff=cutoff,
            sources=sources,
            purge_sources=purge_sources,
            requested_homes=requested_homes,
        )
    except RetentionLockUnavailable as exc:
        report["retention_lock"] = exc.state
        report["status"] = "blocked"
        report["error"] = str(exc)
        _write_report(report, output_format=args.output_format)
        return 1


def _commit_source_identities(source_id: str, identities: frozenset[str]) -> None:
    provider_for_source_id(source_id)
    save_pending_journal(load_pending_journal().merge(source_id, identities))


def _record_reconciliation_outcome(source_id: str, identities: set[str], reconciliation: dict) -> None:
    if not identities:
        return
    journal = load_pending_journal()
    if reconciliation.get("error"):
        save_pending_journal(journal.with_error(str(reconciliation["error"])))
        return
    save_pending_journal(journal.without_identities(source_id, identities))


def _run_retention(
    args,
    report: dict,
    *,
    cutoff: datetime,
    sources: tuple[ArchiveSource, ...],
    purge_sources: tuple[PurgeSource, ...],
    requested_homes: set[str],
) -> int:
    try:
        journal = load_pending_journal()
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        if args.apply:
            report["status"] = "blocked"
            report["error"] = f"pending reconciliation journal unreadable:{type(exc).__name__}:{exc}"
            _write_report(report, output_format=args.output_format)
            return 1
        report["pending_reconciliation"] = {"error": f"{type(exc).__name__}:{exc}"}
        journal = PendingReconciliationJournal()
    else:
        report["pending_reconciliation"] = _journal_report(journal)

    if args.apply:
        try:
            report["quarantine_recovery"] = recover_leftover_quarantine_transactions(
                [source.root for source in sources],
                journal,
            )
        except QuarantineRecoveryError as exc:
            report["status"] = "blocked"
            report["error"] = f"quarantine intent recovery failed:{exc}"
            _write_report(report, output_format=args.output_format)
            return 1

    try:
        report["archive_security"] = secure_archive_storage(ARCHIVE_ROOT, apply=args.apply)
    except OSError as exc:
        report["archive_security"] = {
            "root": str(ARCHIVE_ROOT),
            "applied": False,
            "error": f"{type(exc).__name__}:{exc}",
        }
        if args.apply:
            report["status"] = "blocked"
            report["error"] = "archive permissions could not be secured; no historical data was removed"
            _write_report(report, output_format=args.output_format)
            return 1

    # Extraire les tokens AVANT l'archive des sessions et la purge sqlite.
    # L'extraction est additive : un vrai dry-run ne doit donc jamais l'appeler.
    destructive_data_selected = args.only in {None, "sessions", "logs", "traces"}
    report["llm_usage"] = (
        _extract_llm_usage_before_purge()
        if args.apply and destructive_data_selected
        else {"applied": False, "appended": 0, "reason": "dry_run" if not args.apply else "not_required"}
    )
    if args.apply and report["llm_usage"].get("error"):
        report["status"] = "blocked"
        report["error"] = "llm usage extraction failed; no historical agent data was removed"
        _write_report(report, output_format=args.output_format)
        return 1

    candidate_ids_by_source: dict[str, set[str]] = defaultdict(set)
    removed_ids_by_source: dict[str, set[str]] = defaultdict(set)
    for source in sources:
        if requested_homes and source.source_id not in requested_homes:
            continue
        selected = (
            args.only is None
            or args.only == source.category
            or (args.only == "traces" and source.category == "snapshots")
        )
        if not selected:
            continue
        grouped, protected = collect_source_units(source, cutoff)
        if source.layout in {"acpx_sessions", "grok_sessions"}:
            candidate_ids_by_source[source.source_id].update(
                unit.identity for units in grouped.values() for unit in units if unit.identity is not None
            )
        report["protected"].extend(
            {"source_id": source.source_id, "category": source.category, **entry} for entry in protected
        )
        for month, units in sorted(grouped.items()):
            try:
                session_layout = source.layout in {"acpx_sessions", "grok_sessions"}
                entry = archive_month(
                    units,
                    _archive_destination(source, month),
                    apply=args.apply,
                    source_id=source.source_id,
                    commit_identities=(
                        (
                            lambda identities, source_id=source.source_id: _commit_source_identities(
                                source_id, identities
                            )
                        )
                        if args.apply and session_layout
                        else None
                    ),
                )
                removed_identities = set(entry.pop("removed_identities", []))
                removed_ids_by_source[source.source_id].update(removed_identities)
                entry["removed_identity_count"] = len(removed_identities)
                entry.update(
                    source_id=source.source_id,
                    category=source.category,
                    root=str(source.root),
                    month=month,
                )
            except Exception as exc:  # noqa: BLE001 - retain source and expose partial failure
                entry = {
                    "source_id": source.source_id,
                    "category": source.category,
                    "root": str(source.root),
                    "month": month,
                    "applied": False,
                    "error": f"{type(exc).__name__}:{exc}",
                }
                report["status"] = "partial_failure"
            report["archives"].append(entry)
            if source.category == "sessions":
                report["sessions"].append(entry)

    if args.only in {None, "sessions"}:
        try:
            journal = load_pending_journal()
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError):
            journal = PendingReconciliationJournal()
        report["pending_reconciliation"] = _journal_report(journal)
        if not requested_homes or "acpx" in requested_homes:
            identities = (
                set(removed_ids_by_source["acpx"]) | set(journal.identities_for("acpx"))
                if args.apply
                else candidate_ids_by_source["acpx"]
            )
            reconciliation = reconcile_acpx_session_index(
                ACPX_SESSIONS,
                identities,
                apply=args.apply,
            )
            report["metadata_reconciliation"].append(reconciliation)
            if args.apply:
                _record_reconciliation_outcome("acpx", identities, reconciliation)
                if reconciliation.get("error"):
                    report["status"] = "partial_failure"
        for source_id in ("grok-home", "grok-home-medium"):
            if requested_homes and source_id not in requested_homes:
                continue
            identities = (
                set(removed_ids_by_source[source_id]) | set(journal.identities_for(source_id))
                if args.apply
                else candidate_ids_by_source[source_id]
            )
            reconciliation = reconcile_grok_session_metadata(
                CODEX_HOME.parent / source_id,
                identities,
                apply=args.apply,
            )
            report["metadata_reconciliation"].append(reconciliation)
            if args.apply:
                _record_reconciliation_outcome(source_id, identities, reconciliation)
                if reconciliation.get("error"):
                    report["status"] = "partial_failure"
        try:
            report["pending_reconciliation"] = _journal_report(load_pending_journal())
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            report["pending_reconciliation"] = {"error": f"{type(exc).__name__}:{exc}"}
            if args.apply:
                report["status"] = "partial_failure"

    if args.only in {None, "cache"}:
        for source in purge_sources:
            if requested_homes and source.source_id not in requested_homes:
                continue
            entry = purge_generated_files(source, cutoff, apply=args.apply)
            report["purges"].append(entry)
            if args.apply and entry.get("error"):
                report["status"] = "partial_failure"

    if args.only in {None, "logs"} and (not requested_homes or "codex-home" in requested_homes):
        report["logs"] = purge_codex_logs(CODEX_HOME, apply=args.apply)
        if args.apply and any(item.get("error") for item in report["logs"]):
            report["status"] = "partial_failure"

    candidate_bytes = sum(entry.get("raw_size", 0) for entry in report["archives"])
    candidate_bytes += sum(entry.get("size", 0) for entry in report["purges"])
    candidate_bytes += sum(
        sum(item.get("matched_bytes", 0) for item in reconciliation.get("prompt_histories", []))
        for reconciliation in report["metadata_reconciliation"]
    )
    archive_bytes = sum(entry.get("archive_size", 0) for entry in report["archives"])
    removed_bytes = sum(entry.get("removed_size", 0) for entry in report["archives"])
    removed_bytes += sum(entry.get("size", 0) for entry in report["purges"] if entry.get("applied"))
    removed_bytes += sum(
        sum(item.get("matched_bytes", 0) for item in reconciliation.get("prompt_histories", []))
        + int((reconciliation.get("session_search") or {}).get("reclaimed_bytes", 0))
        for reconciliation in report["metadata_reconciliation"]
        if reconciliation.get("applied")
    )
    reclaimed = removed_bytes - archive_bytes
    reclaimed += sum(
        entry["size"] - entry.get("size_after", entry["size"]) for entry in report["logs"] if "size" in entry
    )
    report["totals"] = {
        "candidate_bytes": candidate_bytes,
        "archive_bytes": archive_bytes,
        "actual_reclaimed_bytes": max(0, reclaimed),
    }
    report["reclaimed_human"] = _human(max(0, reclaimed))

    _write_report(report, output_format=args.output_format)
    return 1 if report["status"] == "partial_failure" else 0


if __name__ == "__main__":
    raise SystemExit(main())
