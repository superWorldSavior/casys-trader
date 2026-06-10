"""Tests unitaires de trader.cockpit_supervisor — logique pure, zéro I/O externe.

Couverture spec complète :
- daemon_vital_state : pid+identité, pas de seuil temporel pour vivant/mort
- launch_daemon : identité cmdline, anti-double daemon manuel, lockfile, DEVNULL, rotation log
- stop_daemon : vérification d'identité avant SIGINT, jamais de signal à pid étranger
- toggle_kill_switch : toggle idempotent
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trader.cockpit_supervisor import (
    daemon_vital_state,
    launch_daemon,
    stop_daemon,
    toggle_kill_switch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DAEMON_CMDLINE = "uv run python -m trader.daemon --live"


def _write_status(path: Path, pid: int, ts_offset_s: float = 0.0) -> None:
    """Écrit un daemon_status.json minimaliste avec pid et ts."""
    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=ts_offset_s)).isoformat()
    path.write_text(
        json.dumps({"ts": ts, "phase": "idle", "pid": pid}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# daemon_vital_state — nouvelle sémantique pid+identité
# ---------------------------------------------------------------------------


def test_vital_pid_vivant_identite_ok_returns_alive(tmp_path, monkeypatch):
    """pid dans status vivant + cmdline trader.daemon → ALIVE, battement_seconds fourni."""
    status_file = tmp_path / "daemon_status.json"
    target_pid = 54321
    _write_status(status_file, pid=target_pid, ts_offset_s=60)

    def fake_kill(pid, sig):
        if sig != 0:
            raise AssertionError(f"ne doit pas envoyer sig={sig}")
        # pid vivant

    def fake_cmdline(pid):
        return "uv run python -m trader.daemon --live"

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", fake_kill)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", fake_cmdline)

    state = daemon_vital_state(status_file)
    assert state.status == "alive"
    assert state.since_seconds is not None  # battement_seconds (âge du ts)


def test_vital_pid_vivant_identite_ok_battement_ancien_returns_alive_busy(tmp_path, monkeypatch):
    """pid vivant + identité OK + battement > 3 min → ALIVE avec battement_old=True."""
    status_file = tmp_path / "daemon_status.json"
    target_pid = 54321
    _write_status(status_file, pid=target_pid, ts_offset_s=600)  # 10 min

    def fake_kill(pid, sig):
        pass  # vivant

    def fake_cmdline(pid):
        return "uv run python -m trader.daemon --live"

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", fake_kill)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", fake_cmdline)

    state = daemon_vital_state(status_file)
    # Pid vivant + identité OK → ALIVE (pas STOPPED même si battement vieux)
    assert state.status == "alive"
    assert state.since_seconds >= 600
    assert state.battement_old is True


def test_vital_pid_vivant_identite_ok_battement_recent_battement_old_false(tmp_path, monkeypatch):
    """pid vivant + battement < 3 min → battement_old=False."""
    status_file = tmp_path / "daemon_status.json"
    _write_status(status_file, pid=42, ts_offset_s=30)

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    state = daemon_vital_state(status_file)
    assert state.battement_old is False


def test_vital_pid_mort_returns_stopped(tmp_path, monkeypatch):
    """pid dans status mort → STOPPED même si battement récent."""
    status_file = tmp_path / "daemon_status.json"
    _write_status(status_file, pid=99999, ts_offset_s=10)

    def fake_kill(pid, sig):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", fake_kill)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "")

    state = daemon_vital_state(status_file)
    assert state.status == "stopped"


def test_vital_pid_vivant_mauvaise_identite_returns_stopped(tmp_path, monkeypatch):
    """pid vivant mais cmdline étrangère (ex: pytest) → STOPPED."""
    status_file = tmp_path / "daemon_status.json"
    _write_status(status_file, pid=os.getpid(), ts_offset_s=10)

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "/usr/bin/python pytest")

    state = daemon_vital_state(status_file)
    assert state.status == "stopped"


def test_vital_no_pid_in_status_returns_never_started(tmp_path):
    """Status sans champ pid → never_started (format pré-migration)."""
    status_file = tmp_path / "daemon_status.json"
    status_file.write_text(json.dumps({"ts": datetime.now(UTC).isoformat(), "phase": "idle"}), encoding="utf-8")

    state = daemon_vital_state(status_file)
    assert state.status == "never_started"


def test_vital_missing_file_returns_never_started():
    """Fichier absent → never_started."""
    state = daemon_vital_state(Path("/tmp/inexistant_daemon_status_xyz.json"))
    assert state.status == "never_started"
    assert state.since_seconds is None


def test_vital_unparsable_ts_returns_stopped_failsafe(tmp_path, monkeypatch):
    """ts illisible mais pid vivant+identité OK → ALIVE quand même (ts non bloquant pour vivant)."""
    status_file = tmp_path / "daemon_status.json"
    status_file.write_text(json.dumps({"ts": "pas-une-date", "phase": "idle", "pid": 42}), encoding="utf-8")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    # pid vivant + identité → ALIVE même si ts unparsable ; since_seconds = None
    state = daemon_vital_state(status_file)
    assert state.status == "alive"
    assert state.since_seconds is None  # inconnu faute de ts


def test_vital_missing_ts_key_pid_vivant_returns_alive(tmp_path, monkeypatch):
    """Pas de clé ts, pid vivant + identité OK → ALIVE."""
    status_file = tmp_path / "daemon_status.json"
    status_file.write_text(json.dumps({"phase": "idle", "pid": 42}), encoding="utf-8")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    state = daemon_vital_state(status_file)
    assert state.status == "alive"


# ---------------------------------------------------------------------------
# launch_daemon — identité, anti-double daemon manuel, lockfile, DEVNULL
# ---------------------------------------------------------------------------


def test_launch_daemon_pid_mort_appelle_popen(tmp_path, monkeypatch):
    """PID mort → Popen avec start_new_session=True, cwd=ROOT, stdin=DEVNULL, close_fds=True."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            self.pid = 99999

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    assert result.launched is True
    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    assert calls[0]["cwd"] == root
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert calls[0]["close_fds"] is True
    # pid file n'est PAS écrit par le cockpit (écrit par le daemon lui-même)
    assert not pid_file.exists()


def test_launch_daemon_pid_vivant_identite_ok_ne_relance_pas(tmp_path, monkeypatch):
    """Pid vivant + cmdline trader.daemon → launched=False (daemon déjà là)."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    target_pid = 12345
    pid_file.write_text(str(target_pid), encoding="utf-8")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    popen_called = []
    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", lambda *a, **kw: popen_called.append(1))

    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    assert result.launched is False
    assert result.reason == "already_running"
    assert popen_called == []


def test_launch_daemon_pid_vivant_identite_etrangere_lance(tmp_path, monkeypatch):
    """Pid vivant mais cmdline étrangère (ex: pytest) → PAS notre daemon → on lance."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    # Pid du process test — vivant mais PAS trader.daemon
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "/usr/bin/python -m pytest")

    calls = []

    class FakePopen:
        def __init__(self, *a, **kw):
            calls.append(kw)
            self.pid = 77777

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    assert result.launched is True
    assert len(calls) == 1


def test_launch_daemon_daemon_manuel_status_frais_ne_relance_pas(tmp_path, monkeypatch):
    """Daemon lancé manuellement (status frais, pid vivant, identité OK, pas de pid_file) → launched=False."""
    pid_file = tmp_path / "daemon.pid"  # absent — lancé manuellement
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    daemon_pid = 88888
    _write_status(status_file, pid=daemon_pid, ts_offset_s=30)

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    popen_called = []
    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", lambda *a, **kw: popen_called.append(1))

    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    assert result.launched is False
    assert result.reason == "already_running"
    assert popen_called == []


def test_launch_daemon_pidfile_corrompu_traite_comme_absent(tmp_path, monkeypatch):
    """pid file corrompu (contenu non-int) → traité comme absent → on lance."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("NOT_A_PID", encoding="utf-8")
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    calls = []

    class FakePopen:
        def __init__(self, *a, **kw):
            calls.append(kw)
            self.pid = 11111

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)
    # status absent → pas de daemon manuel
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "")

    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    assert result.launched is True
    assert len(calls) == 1


def test_launch_daemon_stdout_stderr_vers_log_file(tmp_path, monkeypatch):
    """Popen reçoit stdout et stderr redirigés vers log_file."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    captured = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            captured.update(kw)
            self.pid = 55555

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    assert captured["stdout"] is not None
    assert captured["stderr"] is not None
    assert log_file.exists()


def test_launch_daemon_rotation_log_si_depasse_max(tmp_path, monkeypatch):
    """Si daemon_console.log > MAX_LOG_SIZE_BYTES, le log est tronqué (rotation)."""
    from trader.cockpit_supervisor import MAX_LOG_SIZE_BYTES

    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    # Crée un log dépassant la limite
    big_content = b"x" * (MAX_LOG_SIZE_BYTES + 100)
    log_file.write_bytes(big_content)
    assert log_file.stat().st_size > MAX_LOG_SIZE_BYTES

    class FakePopen:
        def __init__(self, *a, **kw):
            self.pid = 22222

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    # Après rotation, la taille doit être <= MAX_LOG_SIZE_BYTES (exactement MAX si > MAX)
    assert log_file.stat().st_size <= MAX_LOG_SIZE_BYTES


def test_launch_daemon_utilise_lockfile(tmp_path, monkeypatch):
    """launch_daemon acquiert le lock state/daemon.lock avant spawn."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path
    status_file = tmp_path / "daemon_status.json"

    lock_ops = []
    real_flock = fcntl.flock

    def spy_flock(fd, op):
        lock_ops.append(op)
        return real_flock(fd, op)

    monkeypatch.setattr("trader.cockpit_supervisor.fcntl.flock", spy_flock)

    class FakePopen:
        def __init__(self, *a, **kw):
            self.pid = 33333

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    launch_daemon(pid_file=pid_file, log_file=log_file, root=root, status_file=status_file)

    # LOCK_EX (acquire) + LOCK_UN (release) doivent être présents
    assert fcntl.LOCK_EX in lock_ops
    assert fcntl.LOCK_UN in lock_ops


# ---------------------------------------------------------------------------
# stop_daemon — identité avant signal
# ---------------------------------------------------------------------------


def test_stop_daemon_envoie_sigint_au_daemon_identifie(tmp_path, monkeypatch):
    """pid présent + identité trader.daemon → SIGINT envoyé."""
    pid_file = tmp_path / "daemon.pid"
    target_pid = 77777
    pid_file.write_text(str(target_pid), encoding="utf-8")

    signals_sent = []
    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: signals_sent.append((p, s)))
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is True
    real_signals = [(p, s) for p, s in signals_sent if s != 0]
    assert real_signals == [(target_pid, signal.SIGINT)]


def test_stop_daemon_pid_etranger_aucun_signal(tmp_path, monkeypatch):
    """pid vivant mais cmdline étrangère → AUCUN signal envoyé, stopped=False."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    signals_sent = []
    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: signals_sent.append((p, s)))
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "/usr/bin/python pytest")

    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is False
    assert result.reason == "identity_mismatch"
    # Aucun signal SIGINT — seul signal 0 (probe) éventuellement
    real_signals = [(p, s) for p, s in signals_sent if s != 0]
    assert real_signals == []


def test_stop_daemon_pid_absent_ne_signale_pas(tmp_path, monkeypatch):
    """Pas de daemon.pid → stopped=False, aucun signal."""
    pid_file = tmp_path / "daemon.pid"

    signals_sent = []
    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: signals_sent.append((p, s)))

    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is False
    assert signals_sent == []


def test_stop_daemon_pid_mort_ne_signale_pas(tmp_path, monkeypatch):
    """PID mort → stopped=False, aucun signal."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("99999999", encoding="utf-8")

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError("no such process")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", fake_kill)

    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is False


# ---------------------------------------------------------------------------
# toggle_kill_switch
# ---------------------------------------------------------------------------


def test_toggle_kill_switch_cree_fichier_si_absent(tmp_path):
    """KILL absent → le crée, retourne True."""
    kill_file = tmp_path / "KILL"
    active = toggle_kill_switch(kill_file=kill_file)
    assert active is True
    assert kill_file.exists()


def test_toggle_kill_switch_retire_fichier_si_present(tmp_path):
    """KILL présent → le retire, retourne False."""
    kill_file = tmp_path / "KILL"
    kill_file.touch()
    active = toggle_kill_switch(kill_file=kill_file)
    assert active is False
    assert not kill_file.exists()


def test_toggle_kill_switch_deux_toggles_etat_initial(tmp_path):
    """Deux toggles → retour à l'état initial."""
    kill_file = tmp_path / "KILL"
    toggle_kill_switch(kill_file=kill_file)
    active = toggle_kill_switch(kill_file=kill_file)
    assert active is False
    assert not kill_file.exists()
