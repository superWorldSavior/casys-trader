"""cockpit_supervisor — Logique pure superviseur du daemon (sans import Textual).

Responsabilités :
- Calculer l'état vital du daemon depuis daemon_status.json (pid+identité, pas de seuil temporel)
- Lancer le daemon en process détaché (anti-double-lancement : pid file + daemon manuel via status)
- Arrêter le daemon via SIGINT après vérification d'identité (jamais SIGKILL)
- Toggler le kill-switch (fichier KILL)

Ce module n'importe RIEN de Textual — 100 % testable en pytest synchrone.

Rotation daemon_console.log :
    Si daemon_console.log dépasse MAX_LOG_SIZE_BYTES au moment du lancement,
    il est tronqué en gardant la fin (les derniers MAX_LOG_SIZE_BYTES octets).
    Le fichier .1 n'est PAS créé — rotation in-place pour garder la surface minimale.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal as _signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from trader.runtime.pid_file import claim_pid_file as claim_pid_file
from trader.runtime.pid_file import release_pid_file as release_pid_file
from trader.support.system.process_env import sanitized_runtime_env

# Taille maximale de daemon_console.log avant rotation (5 Mo)
MAX_LOG_SIZE_BYTES: int = 5 * 1024 * 1024
UTC = timezone.utc

# Marqueur de cmdline attendu pour identifier un daemon trader
_DAEMON_MARKER = "trader.daemon"


# ---------------------------------------------------------------------------
# Helpers : identité process
# ---------------------------------------------------------------------------


def _get_cmdline(pid: int) -> str:
    """Retourne la cmdline du process pid via ps (POSIX/macOS).

    Retourne chaîne vide si le process n'existe pas ou erreur.
    Jamais de raise.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _is_daemon_pid(pid: int) -> bool:
    """Retourne True si le process pid est vivant ET identifié comme trader.daemon."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return _DAEMON_MARKER in _get_cmdline(pid)


# ---------------------------------------------------------------------------
# daemon_vital_state
# ---------------------------------------------------------------------------


@dataclass
class DaemonVitalState:
    """État vital calculé depuis daemon_status.json.

    status:
        "alive"         — pid dans le status est vivant et identifié trader.daemon
        "stopped"       — pid mort ou identité KO
        "never_started" — fichier absent ou pas de champ pid

    since_seconds:
        Âge du dernier battement (ts dans le status), en secondes.
        None si ts absent/illisible.

    battement_old:
        True si since_seconds >= 180 (battement > 3 min).
        Pertinent uniquement quand status == "alive" (daemon occupé sur batch long).
    """

    status: Literal["alive", "stopped", "never_started"]
    since_seconds: float | None = None
    battement_old: bool = False


def daemon_vital_state(
    status_file: Path,
    *,
    now: datetime | None = None,
) -> DaemonVitalState:
    """Calcule l'état vital du daemon depuis daemon_status.json.

    Logique :
      1. Fichier absent → never_started
      2. Pas de champ "pid" → never_started (format pré-migration)
      3. Pid mort ou cmdline étrangère → stopped
      4. Pid vivant + identité trader.daemon → alive
         since_seconds = âge du battement (None si ts illisible)
         battement_old = True si since_seconds >= 180

    Fail-safe : les erreurs de parsing/I/O ne produisent jamais "alive" par
    défaut — elles produisent "stopped" ou "never_started" selon le contexte.

    Args:
        status_file: Chemin vers state/daemon_status.json.
        now: Instant de référence (injecté pour tests déterministes).
    """
    if now is None:
        now = datetime.now(UTC)

    if not status_file.exists():
        return DaemonVitalState(status="never_started")

    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception:
        return DaemonVitalState(status="stopped")

    pid = data.get("pid")
    if not pid:
        return DaemonVitalState(status="never_started")

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return DaemonVitalState(status="stopped")

    # Vérification identité
    if not _is_daemon_pid(pid):
        return DaemonVitalState(status="stopped")

    # Pid vivant + identité OK → alive
    # Calcul du battement
    since_seconds: float | None = None
    battement_old = False
    raw_ts = data.get("ts")
    if raw_ts:
        try:
            ts = datetime.fromisoformat(raw_ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            since_seconds = (now - ts).total_seconds()
            battement_old = since_seconds >= 180
        except Exception:
            pass  # ts illisible → since_seconds reste None

    return DaemonVitalState(status="alive", since_seconds=since_seconds, battement_old=battement_old)


# ---------------------------------------------------------------------------
# launch_daemon
# ---------------------------------------------------------------------------


@dataclass
class LaunchResult:
    """Résultat de launch_daemon."""

    launched: bool
    pid: int | None = None
    reason: str = ""  # "already_running" | "launched" | "identity_mismatch" | "error"


def _rotate_log_if_needed(log_file: Path) -> None:
    """Tronque daemon_console.log si > MAX_LOG_SIZE_BYTES en gardant la fin."""
    if not log_file.exists():
        return
    size = log_file.stat().st_size
    if size <= MAX_LOG_SIZE_BYTES:
        return
    # Garder les derniers MAX_LOG_SIZE_BYTES octets
    with log_file.open("rb") as fh:
        fh.seek(-MAX_LOG_SIZE_BYTES, 2)
        tail = fh.read()
    log_file.write_bytes(tail)


def launch_daemon(
    *,
    pid_file: Path,
    log_file: Path,
    root: Path,
    status_file: Path | None = None,
    command: list[str] | None = None,
) -> LaunchResult:
    """Lance le daemon en process détaché si pas déjà vivant.

    Anti-double-lancement (deux sources) :
    1. pid_file (state/daemon.pid) — mis à jour par le daemon lui-même au démarrage.
    2. status_file (state/daemon_status.json) — vérifie un daemon manuel (lancé hors cockpit).

    Dans les deux cas, la vérification d'identité (_DAEMON_MARKER dans cmdline) est requise.

    Atomicité : acquiert state/daemon.lock (fcntl.flock LOCK_EX) autour de la séquence
    check+spawn pour empêcher deux cockpits de spawner simultanément.

    Le pid file est écrit par le daemon lui-même (via main()) — le cockpit ne l'écrit PLUS.

    Args:
        pid_file: Chemin vers state/daemon.pid (lu, pas écrit par le cockpit).
        log_file: Chemin vers state/daemon_console.log (stdout+stderr en append, rotaté si besoin).
        root: Répertoire racine du repo (cwd du process lancé).
        status_file: Chemin vers state/daemon_status.json (pour détecter un daemon manuel).
        command: Commande à exécuter (défaut : uv run python -m trader.daemon --live).

    Returns:
        LaunchResult(launched=True) si spawn lancé,
        LaunchResult(launched=False, reason="already_running") si daemon déjà actif.
    """
    if command is None:
        command = ["uv", "run", "python", "-m", "trader.daemon", "--live"]

    lock_path = pid_file.parent / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # O_NOFOLLOW : refuse de suivre un symlink (ex. daemon.lock → config/risk.yaml
    # tronquerait la cible). O_CREAT crée le fichier s'il est absent.
    try:
        _lock_fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # lock_path est un symlink — refus de suivre pour ne pas altérer la cible.
            return LaunchResult(launched=False, reason="lock_is_symlink")
        raise
    lock_fh = os.fdopen(_lock_fd, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)

        # Source 1 : pid_file (daemon lancé par cockpit précédemment)
        if pid_file.exists():
            try:
                existing_pid = int(pid_file.read_text(encoding="utf-8").strip())
                if _is_daemon_pid(existing_pid):
                    return LaunchResult(launched=False, pid=existing_pid, reason="already_running")
            except (ValueError, OSError):
                pass  # pid file corrompu → on continue

        # Source 2 : daemon_status.json (daemon lancé manuellement hors cockpit)
        if status_file is not None and status_file.exists():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                manual_pid = data.get("pid")
                if manual_pid:
                    manual_pid = int(manual_pid)
                    if _is_daemon_pid(manual_pid):
                        return LaunchResult(launched=False, pid=manual_pid, reason="already_running")
            except (ValueError, OSError, TypeError):
                pass

        # Rotation log si nécessaire
        _rotate_log_if_needed(log_file)

        # Ouvre le fichier log en append (crée si absent)
        log_handle = log_file.open("a", encoding="utf-8")

        proc = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=root,
            env=sanitized_runtime_env(),
        )

        return LaunchResult(launched=True, pid=proc.pid, reason="launched")

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


# ---------------------------------------------------------------------------
# stop_daemon
# ---------------------------------------------------------------------------


@dataclass
class StopResult:
    """Résultat de stop_daemon."""

    stopped: bool
    pid: int | None = None
    reason: str = ""  # "sigint_sent" | "no_daemon" | "pid_dead" | "identity_mismatch"


def _pid_from_status(status_file: Path | None) -> int | None:
    """Pid du daemon depuis daemon_status.json (réécrit à chaque cycle)."""
    if status_file is None or not status_file.exists():
        return None
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        raw = data.get("pid")
        return int(raw) if raw else None
    except (ValueError, OSError):
        return None


def stop_daemon(*, pid_file: Path, status_file: Path | None = None) -> StopResult:
    """Envoie SIGINT au daemon après vérification d'identité. Jamais SIGKILL.

    Candidats pid (ordonnés, dédupliqués) : status_file PUIS pid_file.
    Pour chaque candidat, le premier qui passe _is_daemon_pid reçoit SIGINT.
    Invariant : un pid qui échoue le check d'identité ne reçoit JAMAIS de signal.

    Ce double-fallback couvre le scénario :
      - pid_file contient un pid STALE B (mort ou process étranger)
      - daemon_status.json pointe le vrai daemon vivant A
    → stop envoie SIGINT à A, pas à B.

    Args:
        pid_file: Chemin vers state/daemon.pid.
        status_file: Chemin vers state/daemon_status.json (fallback optionnel).

    Returns:
        StopResult(stopped=True, reason="sigint_sent") si signal envoyé,
        StopResult(stopped=False) sinon.
    """
    # Construire la liste ordonnée et dédupliquée des candidats pid.
    # status_file D'ABORD : écrit par le daemon lui-même à chaque phase, c'est
    # la source la plus fraîche. Un daemon.pid stale pointant vers un AUTRE
    # daemon casys vivant (autre worktree) passerait le check d'identité —
    # l'ordre status→pid_file évite de SIGINT le mauvais process.
    candidates: list[int] = []
    seen: set[int] = set()

    pid_status = _pid_from_status(status_file)
    if pid_status is not None:
        candidates.append(pid_status)
        seen.add(pid_status)

    if pid_file.exists():
        try:
            pid_val = int(pid_file.read_text(encoding="utf-8").strip())
            if pid_val not in seen:
                candidates.append(pid_val)
                seen.add(pid_val)
        except (ValueError, OSError):
            pass

    if not candidates:
        return StopResult(stopped=False, reason="no_daemon")

    # Premier candidat passant le check d'identité reçoit SIGINT.
    # Un candidat qui échoue n'est JAMAIS signalé (invariant conservé).
    for pid in candidates:
        if _is_daemon_pid(pid):
            os.kill(pid, _signal.SIGINT)
            return StopResult(stopped=True, pid=pid, reason="sigint_sent")

    # Aucun candidat n'a passé le check — affiner la raison d'échec
    for pid in candidates:
        try:
            os.kill(pid, 0)
            # Vivant mais identité étrangère
            return StopResult(stopped=False, pid=pid, reason="identity_mismatch")
        except (ProcessLookupError, PermissionError):
            pass

    return StopResult(stopped=False, pid=candidates[0], reason="pid_dead")


# ---------------------------------------------------------------------------
# toggle_kill_switch
# ---------------------------------------------------------------------------


def toggle_kill_switch(*, kill_file: Path) -> bool:
    """Toggle le fichier KILL.

    Suppression : os.unlink opère sur le chemin lui-même sans jamais suivre les
    symlinks (os.path.islink documente ce comportement) — un lien KILL → cible
    arbitraire supprime uniquement le lien, la cible n'est pas touchée.

    Création : os.open avec O_CREAT|O_EXCL|O_NOFOLLOW — atomique (pas de TOCTOU
    entre deux cockpits) et sans suivi de symlink. Si FileExistsError (race), le
    kill-switch est déjà actif → retourne True sans crash.

    Args:
        kill_file: Chemin vers le fichier KILL racine du repo.

    Returns:
        True si le kill-switch est maintenant actif (fichier créé),
        False s'il a été retiré.
    """
    path_str = str(kill_file)

    # lexists ne suit pas les symlinks : un lien mort ou vivant vaut True
    if os.path.lexists(path_str):
        # Suppression. os.unlink est toujours no-follow : il opère sur le chemin
        # lui-même. os.path.islink(path_str) serait True pour un symlink ici —
        # dans tous les cas la cible n'est jamais modifiée.
        os.unlink(path_str)
        return False

    # Création atomique no-follow
    try:
        fd = os.open(path_str, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW)
        os.close(fd)
        return True
    except FileExistsError:
        # Race : un autre cockpit a créé le fichier entre lexists et open.
        # Le kill-switch est actif.
        return True
