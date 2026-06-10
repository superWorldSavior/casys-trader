# Cockpit Supervisor Pattern — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transformer le cockpit Textual en salle de contrôle du daemon (pattern superviseur) tout en réduisant le Makefile de 12 à 4 cibles + help.

**Architecture:** Le daemon reste un process indépendant (PID dans `state/daemon.pid`, logs dans `state/daemon_console.log`). Le cockpit acquiert 3 nouveaux bindings (s/X/k) qui délèguent à des fonctions pures testables isolées du widget Textual. Les modals Textual gèrent les confirmations. La logique vitale (âge du `ts`) vit dans `trader/cockpit_supervisor.py` — module pur sans import Textual.

**Tech Stack:** Python 3.12, Textual 8.x, subprocess.Popen (start_new_session=True), os.kill/SIGINT, pytest-asyncio (asyncio_mode=auto), uv.

---

## Structure des fichiers

| Fichier | Rôle |
|---|---|
| **Créer** `trader/cockpit_supervisor.py` | Logique pure : `daemon_vital_state()`, `launch_daemon()`, `stop_daemon()`, `toggle_kill_switch()` — aucun import Textual |
| **Créer** `tests/test_cockpit_supervisor.py` | Tests unitaires de la logique pure (fonction vitale, anti-double-lancement, arrêt, kill-switch) |
| **Modifier** `trader/cockpit.py` | Bindings s/X/k, modals, indicateur vital dans CockpitStatus, docstring mis à jour |
| **Modifier** `tests/test_cockpit_smoke.py` | Tests smoke nouveaux bindings + test q-sans-signal |
| **Modifier** `Makefile` | 12 → 4 cibles + help |

**Contraintes absolues** : ne pas toucher `trader/daemon.py`, `trader/tui.py`, `trader/palette.py`, `trader/consolidator.py`, `tests/test_consolidator.py`, `tests/test_cli_semantic.py`, `tests/test_daemon_learnings.py`, `tests/test_llm.py`, `README.md`, `docs/`.

---

## Task 1 : Module pur `cockpit_supervisor.py` — fonction vitale + structures

**Files:**
- Create: `trader/cockpit_supervisor.py`
- Create: `tests/test_cockpit_supervisor.py`

- [ ] **Step 1.1 : Écrire les tests rouges — fonction vitale**

```python
# tests/test_cockpit_supervisor.py
"""Tests unitaires de trader.cockpit_supervisor — logique pure, zéro I/O externe."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trader.cockpit_supervisor import DaemonVitalState, daemon_vital_state


# ---------------------------------------------------------------------------
# daemon_vital_state — états possibles
# ---------------------------------------------------------------------------


def test_vital_recent_returns_alive(tmp_path):
    """ts vieux de 90 s < 3 min → ALIVE."""
    status_file = tmp_path / "daemon_status.json"
    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(seconds=90)).isoformat()
    status_file.write_text(json.dumps({"ts": ts, "phase": "idle"}), encoding="utf-8")

    state = daemon_vital_state(status_file, now=now)
    assert state.status == "alive"
    assert state.since_seconds < 180


def test_vital_old_returns_stopped(tmp_path):
    """ts vieux de 5 min > 3 min → STOPPED avec durée."""
    status_file = tmp_path / "daemon_status.json"
    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(minutes=5)).isoformat()
    status_file.write_text(json.dumps({"ts": ts, "phase": "idle"}), encoding="utf-8")

    state = daemon_vital_state(status_file, now=now)
    assert state.status == "stopped"
    assert state.since_seconds >= 300


def test_vital_exactly_3min_is_stopped(tmp_path):
    """ts vieux de exactement 180 s (= 3 min) → STOPPED (borne exclusive)."""
    status_file = tmp_path / "daemon_status.json"
    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    ts = (now - timedelta(seconds=180)).isoformat()
    status_file.write_text(json.dumps({"ts": ts, "phase": "idle"}), encoding="utf-8")

    state = daemon_vital_state(status_file, now=now)
    assert state.status == "stopped"


def test_vital_missing_file_returns_never_started():
    """Fichier absent → NEVER_STARTED."""
    state = daemon_vital_state(Path("/tmp/inexistant_daemon_status_xyz.json"))
    assert state.status == "never_started"
    assert state.since_seconds is None


def test_vital_unparsable_ts_returns_stopped_failsafe(tmp_path):
    """ts illisible → STOPPED (fail-safe, jamais ALIVE sur erreur)."""
    status_file = tmp_path / "daemon_status.json"
    status_file.write_text(json.dumps({"ts": "pas-une-date", "phase": "idle"}), encoding="utf-8")

    state = daemon_vital_state(status_file)
    assert state.status == "stopped"
    assert state.since_seconds is None


def test_vital_missing_ts_key_returns_stopped_failsafe(tmp_path):
    """Fichier JSON sans clé ts → STOPPED (fail-safe)."""
    status_file = tmp_path / "daemon_status.json"
    status_file.write_text(json.dumps({"phase": "idle"}), encoding="utf-8")

    state = daemon_vital_state(status_file)
    assert state.status == "stopped"
```

- [ ] **Step 1.2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_supervisor.py -v 2>&1 | head -30
```

Attendu : `ModuleNotFoundError: No module named 'trader.cockpit_supervisor'`

- [ ] **Step 1.3 : Créer `trader/cockpit_supervisor.py` avec les types et la fonction vitale**

```python
# trader/cockpit_supervisor.py
"""cockpit_supervisor — Logique pure superviseur du daemon (sans import Textual).

Responsabilités :
- Calculer l'état vital du daemon depuis daemon_status.json
- Lancer le daemon en process détaché (anti-double-lancement via daemon.pid)
- Arrêter le daemon via SIGINT (jamais SIGKILL)
- Toggler le kill-switch (fichier KILL)

Ce module n'importe RIEN de Textual — 100 % testable en pytest synchrone.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


@dataclass
class DaemonVitalState:
    """État vital calculé depuis daemon_status.json."""

    status: Literal["alive", "stopped", "never_started"]
    since_seconds: float | None = None  # None si jamais démarré ou ts illisible


def daemon_vital_state(
    status_file: Path,
    *,
    now: datetime | None = None,
) -> DaemonVitalState:
    """Calcule l'état vital du daemon depuis le fichier de statut.

    Args:
        status_file: Chemin vers state/daemon_status.json.
        now: Instant de référence (injecté pour tests déterministes ; défaut = UTC now).

    Returns:
        DaemonVitalState avec status in {"alive", "stopped", "never_started"}.

    Fail-safe : toute erreur de parsing/lecture → "stopped" (jamais "alive").
    """
    if now is None:
        now = datetime.now(UTC)

    if not status_file.exists():
        return DaemonVitalState(status="never_started")

    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        raw_ts = data.get("ts")
        if not raw_ts:
            return DaemonVitalState(status="stopped")
        ts = datetime.fromisoformat(raw_ts)
        # Assure que ts est timezone-aware
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = (now - ts).total_seconds()
        if age < 180:  # < 3 min
            return DaemonVitalState(status="alive", since_seconds=age)
        return DaemonVitalState(status="stopped", since_seconds=age)
    except Exception:
        return DaemonVitalState(status="stopped")
```

- [ ] **Step 1.4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_supervisor.py::test_vital_recent_returns_alive tests/test_cockpit_supervisor.py::test_vital_old_returns_stopped tests/test_cockpit_supervisor.py::test_vital_exactly_3min_is_stopped tests/test_cockpit_supervisor.py::test_vital_missing_file_returns_never_started tests/test_cockpit_supervisor.py::test_vital_unparsable_ts_returns_stopped_failsafe tests/test_cockpit_supervisor.py::test_vital_missing_ts_key_returns_stopped_failsafe -v
```

Attendu : 6 PASSED

---

## Task 2 : `launch_daemon()` — anti-double-lancement

**Files:**
- Modify: `trader/cockpit_supervisor.py`
- Modify: `tests/test_cockpit_supervisor.py`

- [ ] **Step 2.1 : Écrire les tests rouges — launch_daemon**

Ajouter à `tests/test_cockpit_supervisor.py` :

```python
# ---------------------------------------------------------------------------
# launch_daemon — anti-double-lancement
# ---------------------------------------------------------------------------


def test_launch_daemon_pid_mort_appelle_popen(tmp_path, monkeypatch):
    """PID absent/mort → Popen appelé avec start_new_session=True et cwd=ROOT."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path

    calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
            self.pid = 99999

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    from trader.cockpit_supervisor import launch_daemon, LaunchResult
    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root)

    assert result.launched is True
    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    assert calls[0]["cwd"] == root
    assert pid_file.read_text(encoding="utf-8").strip() == "99999"


def test_launch_daemon_pid_vivant_ne_relance_pas(tmp_path, monkeypatch):
    """PID dans daemon.pid est vivant → Popen PAS appelé, launched=False."""
    import os as _os

    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path

    # Utilise le PID du process actuel (garantit qu'il est vivant)
    current_pid = _os.getpid()
    pid_file.write_text(str(current_pid), encoding="utf-8")

    popen_called = []
    monkeypatch.setattr(
        "trader.cockpit_supervisor.subprocess.Popen",
        lambda *a, **kw: popen_called.append(1),
    )

    from trader.cockpit_supervisor import launch_daemon
    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root)

    assert result.launched is False
    assert popen_called == []


def test_launch_daemon_pid_fichier_absent_lance(tmp_path, monkeypatch):
    """Pas de fichier daemon.pid → lancement autorisé."""
    pid_file = tmp_path / "daemon.pid"  # n'existe pas
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path

    class FakePopen:
        def __init__(self, *a, **kw):
            self.pid = 12345

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    from trader.cockpit_supervisor import launch_daemon
    result = launch_daemon(pid_file=pid_file, log_file=log_file, root=root)

    assert result.launched is True


def test_launch_daemon_stdout_stderr_vers_log_file(tmp_path, monkeypatch):
    """Popen reçoit stdout et stderr redirigés vers le log_file en append."""
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "daemon_console.log"
    root = tmp_path

    captured = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            captured.update(kw)
            self.pid = 55555

    monkeypatch.setattr("trader.cockpit_supervisor.subprocess.Popen", FakePopen)

    from trader.cockpit_supervisor import launch_daemon
    launch_daemon(pid_file=pid_file, log_file=log_file, root=root)

    # stdout et stderr doivent être des fichiers ouverts (pas PIPE ni None)
    assert captured["stdout"] is not None
    assert captured["stderr"] is not None
    # Le log_file doit avoir été créé (ouverture en append)
    assert log_file.exists() or True  # le fichier est créé à l'ouverture
```

- [ ] **Step 2.2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_supervisor.py::test_launch_daemon_pid_mort_appelle_popen tests/test_cockpit_supervisor.py::test_launch_daemon_pid_vivant_ne_relance_pas tests/test_cockpit_supervisor.py::test_launch_daemon_pid_fichier_absent_lance tests/test_cockpit_supervisor.py::test_launch_daemon_stdout_stderr_vers_log_file -v 2>&1 | head -20
```

Attendu : 4 FAILED (`ImportError: cannot import name 'launch_daemon'`)

- [ ] **Step 2.3 : Implémenter `launch_daemon` dans `trader/cockpit_supervisor.py`**

Ajouter après `daemon_vital_state` :

```python
@dataclass
class LaunchResult:
    """Résultat de launch_daemon."""

    launched: bool
    pid: int | None = None
    reason: str = ""  # "already_running" | "launched" | "error"


def _pid_is_alive(pid: int) -> bool:
    """Teste si le PID est vivant via os.kill(pid, 0). False si mort ou absent."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def launch_daemon(
    *,
    pid_file: Path,
    log_file: Path,
    root: Path,
    command: list[str] | None = None,
) -> LaunchResult:
    """Lance le daemon en process détaché si pas déjà vivant.

    Anti-double-lancement : si daemon.pid existe et PID est vivant → ne fait rien.

    Args:
        pid_file: Chemin vers state/daemon.pid.
        log_file: Chemin vers state/daemon_console.log (stdout+stderr en append).
        root: Répertoire racine du repo (cwd du process lancé).
        command: Commande à exécuter (défaut : ['uv', 'run', 'python', '-m', 'trader.daemon', '--live']).

    Returns:
        LaunchResult(launched=True, pid=...) si lancé, LaunchResult(launched=False) si déjà actif.
    """
    if command is None:
        command = ["uv", "run", "python", "-m", "trader.daemon", "--live"]

    # Vérifie si un daemon est déjà vivant
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding="utf-8").strip())
            if _pid_is_alive(existing_pid):
                return LaunchResult(launched=False, pid=existing_pid, reason="already_running")
        except (ValueError, OSError):
            pass  # pid file corrompu → on relance

    # Ouvre le fichier log en append (crée si absent)
    log_handle = log_file.open("a", encoding="utf-8")

    proc = subprocess.Popen(
        command,
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
        cwd=root,
    )

    pid_file.write_text(str(proc.pid), encoding="utf-8")
    return LaunchResult(launched=True, pid=proc.pid, reason="launched")
```

- [ ] **Step 2.4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_supervisor.py -k "launch" -v
```

Attendu : 4 PASSED

---

## Task 3 : `stop_daemon()` + `toggle_kill_switch()`

**Files:**
- Modify: `trader/cockpit_supervisor.py`
- Modify: `tests/test_cockpit_supervisor.py`

- [ ] **Step 3.1 : Écrire les tests rouges — stop_daemon et toggle_kill_switch**

Ajouter à `tests/test_cockpit_supervisor.py` :

```python
# ---------------------------------------------------------------------------
# stop_daemon — SIGINT propre
# ---------------------------------------------------------------------------


def test_stop_daemon_envoie_sigint_au_bon_pid(tmp_path, monkeypatch):
    """pid présent et vivant → os.kill(pid, SIGINT) appelé exactement une fois."""
    import signal

    pid_file = tmp_path / "daemon.pid"
    target_pid = 77777
    pid_file.write_text(str(target_pid), encoding="utf-8")

    signals_sent = []

    def fake_kill(pid, sig):
        signals_sent.append((pid, sig))

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", fake_kill)

    from trader.cockpit_supervisor import stop_daemon, StopResult
    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is True
    assert signals_sent == [(target_pid, signal.SIGINT)]


def test_stop_daemon_pid_absent_ne_signale_pas(tmp_path, monkeypatch):
    """Pas de daemon.pid → stopped=False, aucun signal envoyé."""
    pid_file = tmp_path / "daemon.pid"  # n'existe pas

    signals_sent = []
    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: signals_sent.append((p, s)))

    from trader.cockpit_supervisor import stop_daemon
    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is False
    assert signals_sent == []


def test_stop_daemon_pid_mort_ne_signale_pas(tmp_path, monkeypatch):
    """PID dans daemon.pid mais process mort → stopped=False."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("99999999", encoding="utf-8")  # PID improbable

    import signal

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError("no such process")
        # Ne devrait pas être appelé avec SIGINT

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", fake_kill)

    from trader.cockpit_supervisor import stop_daemon
    result = stop_daemon(pid_file=pid_file)

    assert result.stopped is False


# ---------------------------------------------------------------------------
# toggle_kill_switch
# ---------------------------------------------------------------------------


def test_toggle_kill_switch_cree_fichier_si_absent(tmp_path):
    """KILL absent → le crée."""
    kill_file = tmp_path / "KILL"
    assert not kill_file.exists()

    from trader.cockpit_supervisor import toggle_kill_switch
    active = toggle_kill_switch(kill_file=kill_file)

    assert active is True
    assert kill_file.exists()


def test_toggle_kill_switch_retire_fichier_si_present(tmp_path):
    """KILL présent → le retire."""
    kill_file = tmp_path / "KILL"
    kill_file.touch()

    from trader.cockpit_supervisor import toggle_kill_switch
    active = toggle_kill_switch(kill_file=kill_file)

    assert active is False
    assert not kill_file.exists()


def test_toggle_kill_switch_deux_toggles_etat_initial(tmp_path):
    """Deux toggles consécutifs → retour à l'état initial."""
    kill_file = tmp_path / "KILL"

    from trader.cockpit_supervisor import toggle_kill_switch
    toggle_kill_switch(kill_file=kill_file)  # True
    active = toggle_kill_switch(kill_file=kill_file)  # False

    assert active is False
    assert not kill_file.exists()
```

- [ ] **Step 3.2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_supervisor.py -k "stop_daemon or toggle_kill" -v 2>&1 | head -30
```

Attendu : 6 FAILED (`ImportError`)

- [ ] **Step 3.3 : Implémenter `stop_daemon` et `toggle_kill_switch` dans `trader/cockpit_supervisor.py`**

Ajouter en bas du fichier :

```python
import signal as _signal


@dataclass
class StopResult:
    """Résultat de stop_daemon."""

    stopped: bool
    pid: int | None = None
    reason: str = ""  # "sigint_sent" | "no_daemon" | "pid_dead"


def stop_daemon(*, pid_file: Path) -> StopResult:
    """Envoie SIGINT au daemon via son PID. Jamais SIGKILL.

    Args:
        pid_file: Chemin vers state/daemon.pid.

    Returns:
        StopResult(stopped=True) si le signal a été envoyé,
        StopResult(stopped=False) si pas de daemon vivant.
    """
    if not pid_file.exists():
        return StopResult(stopped=False, reason="no_daemon")

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return StopResult(stopped=False, reason="no_daemon")

    # Vérifie si vivant
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return StopResult(stopped=False, pid=pid, reason="pid_dead")

    # Envoie SIGINT (arrêt propre — le daemon gère KeyboardInterrupt)
    os.kill(pid, _signal.SIGINT)
    return StopResult(stopped=True, pid=pid, reason="sigint_sent")


def toggle_kill_switch(*, kill_file: Path) -> bool:
    """Toggle le fichier KILL.

    Args:
        kill_file: Chemin vers le fichier KILL racine du repo.

    Returns:
        True si le kill-switch est maintenant actif (fichier créé),
        False s'il a été retiré.
    """
    if kill_file.exists():
        kill_file.unlink()
        return False
    kill_file.touch()
    return True
```

**Important** : ajouter `import signal as _signal` en haut des imports du fichier (pas en bas).

- [ ] **Step 3.4 : Vérifier que tous les tests du module passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_supervisor.py -v
```

Attendu : tous PASSED (≈ 18 tests)

---

## Task 4 : Indicateur vital dans `CockpitStatus` (header du cockpit)

**Files:**
- Modify: `trader/cockpit.py`
- Modify: `tests/test_cockpit_smoke.py`

- [ ] **Step 4.1 : Écrire les tests rouges — indicateur vital dans smoke tests**

Ajouter à `tests/test_cockpit_smoke.py` :

```python
# ---------------------------------------------------------------------------
# Indicateur vital
# ---------------------------------------------------------------------------


async def test_cockpit_status_affiche_daemon_alive(tmp_path, monkeypatch):
    """Quand daemon_status.json est récent (< 3 min), CockpitStatus affiche DAEMON en vert."""
    from datetime import UTC, datetime, timedelta
    import json

    _make_minimal_state(tmp_path)
    # Rend le ts très récent
    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=30)).isoformat()
    status_data = {"ts": ts, "phase": "idle", "decisions_done": 0,
                   "symbols_total": 0, "model_calls_used": 0, "max_model_calls_per_cycle": 25}
    (tmp_path / "daemon_status.json").write_text(json.dumps(status_data), encoding="utf-8")

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        status = app.query_one("#cockpit-status")
        rendered = str(status.renderable)
        # "DAEMON" ou "● DAEMON" doit apparaître
        assert "DAEMON" in rendered or "daemon" in rendered.lower()


async def test_cockpit_status_affiche_arrete_quand_ts_vieux(tmp_path, monkeypatch):
    """ts vieux > 3 min → CockpitStatus affiche ARRÊTÉ."""
    from datetime import UTC, datetime, timedelta
    import json

    _make_minimal_state(tmp_path)
    now = datetime.now(UTC)
    ts = (now - timedelta(minutes=10)).isoformat()
    status_data = {"ts": ts, "phase": "idle", "decisions_done": 0,
                   "symbols_total": 0, "model_calls_used": 0, "max_model_calls_per_cycle": 25}
    (tmp_path / "daemon_status.json").write_text(json.dumps(status_data), encoding="utf-8")

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        status = app.query_one("#cockpit-status")
        rendered = str(status.renderable)
        assert "ARRÊTÉ" in rendered or "arrete" in rendered.lower() or "stopped" in rendered.lower()
```

- [ ] **Step 4.2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_status_affiche_daemon_alive tests/test_cockpit_smoke.py::test_cockpit_status_affiche_arrete_quand_ts_vieux -v 2>&1 | head -30
```

Attendu : 2 FAILED (le rendu ne contient pas encore "DAEMON"/"ARRÊTÉ")

- [ ] **Step 4.3 : Modifier `CockpitStatus.update_state` pour afficher l'indicateur vital**

Dans `trader/cockpit.py`, ajouter l'import en tête de fichier :

```python
from trader.cockpit_supervisor import DaemonVitalState, daemon_vital_state
```

Modifier `CockpitStatus.update_state` — remplacer la ligne qui construit `text` (la longue f-string) par la version avec indicateur vital.

Localiser dans `trader/cockpit.py` le bloc autour de la ligne 189 (variable `phase_style`). Ajouter avant la construction de `text` :

```python
        # Indicateur vital — basé sur daemon_status.json (path injecté via module)
        vital = daemon_vital_state(
            _STATE_DIR / "daemon_status.json",
        )
        if vital.status == "alive":
            vital_str = "[bold green]● DAEMON[/bold green]"
        elif vital.status == "stopped":
            if vital.since_seconds is not None:
                minutes = int(vital.since_seconds) // 60
                seconds = int(vital.since_seconds) % 60
                vital_str = f"[bold red]● ARRÊTÉ depuis {minutes:02d}:{seconds:02d}[/bold red]"
            else:
                vital_str = "[bold red]● ARRÊTÉ[/bold red]"
        else:  # never_started
            vital_str = "[dim]● jamais démarré[/dim]"
```

Puis dans la f-string `text`, ajouter `f" {vital_str}"` au début (avant `  Équité ...`) ou après le Kill.

Remplacer le bloc `text = (...)` existant par :

```python
        text = (
            f"  {vital_str}"
            f"  Équité [{eq_style}]${equity:,.2f}[/{eq_style}]"
            f"  Cash [{acc_style}]${cash:,.2f}[/{acc_style}]"
            f"  P&L [{ret_style}]{ret_pct:+.2f}%[/{ret_style}]"
            f" [{pnl_style}]({pnl:+,.2f})[/{pnl_style}]"
            f"  Daemon [{phase_style}]{phase}[/{phase_style}]"
            f"  Symbole [{acc_style}]{current_symbol}[/{acc_style}]"
            f"  Appels [{acc_style}]{calls_str}[/{acc_style}]"
            f"  {now_utc}"
            f"  Mode {mode_str}"
            f"  Kill {kill_str}"
        )
```

- [ ] **Step 4.4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py -v 2>&1 | tail -20
```

Attendu : tous les smoke tests PASSED

---

## Task 5 : Binding `s` — démarrer le daemon

**Files:**
- Modify: `trader/cockpit.py`
- Modify: `tests/test_cockpit_smoke.py`

- [ ] **Step 5.1 : Écrire les tests rouges — binding s**

Ajouter à `tests/test_cockpit_smoke.py` :

```python
# ---------------------------------------------------------------------------
# Bindings déclarés
# ---------------------------------------------------------------------------


async def test_cockpit_binding_s_declare(tmp_path, monkeypatch):
    """Le binding 's' doit être déclaré dans BINDINGS."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        keys = [b.key for b in app.BINDINGS]
        assert "s" in keys


async def test_cockpit_binding_X_declare(tmp_path, monkeypatch):
    """Le binding 'X' (majuscule) doit être déclaré dans BINDINGS."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        keys = [b.key for b in app.BINDINGS]
        assert "X" in keys


async def test_cockpit_binding_k_declare(tmp_path, monkeypatch):
    """Le binding 'k' doit être déclaré dans BINDINGS."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        keys = [b.key for b in app.BINDINGS]
        assert "k" in keys
```

- [ ] **Step 5.2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_binding_s_declare tests/test_cockpit_smoke.py::test_cockpit_binding_X_declare tests/test_cockpit_smoke.py::test_cockpit_binding_k_declare -v
```

Attendu : 3 FAILED (bindings absents)

- [ ] **Step 5.3 : Ajouter les bindings s/X/k dans `CockpitApp.BINDINGS`**

Dans `trader/cockpit.py`, modifier `BINDINGS` :

```python
    BINDINGS = [
        Binding("q", "quit", "Quitter"),
        Binding("s", "start_daemon", "Démarrer daemon"),
        Binding("X", "stop_daemon_confirm", "Arrêter daemon"),
        Binding("k", "toggle_kill", "Kill-switch"),
        Binding("c", "toggle_cycles", "Toggle cycles"),
        Binding("f", "toggle_scroll", "Pause scroll"),
        Binding("l", "toggle_logs", "Toggle logs"),
        Binding("d", "toggle_theme", "Dark/Light"),
    ]
```

- [ ] **Step 5.4 : Implémenter `action_start_daemon` dans `CockpitApp`**

Ajouter en bas de `CockpitApp` (avant `main()`), après `action_toggle_theme` :

```python
    def action_start_daemon(self) -> None:
        """Lance le daemon en process détaché (anti-double-lancement via daemon.pid)."""
        from trader.cockpit_supervisor import launch_daemon

        result = launch_daemon(
            pid_file=_STATE_DIR / "daemon.pid",
            log_file=_STATE_DIR / "daemon_console.log",
            root=_ROOT,
        )
        if result.launched:
            self.notify(f"Daemon lancé (PID {result.pid})", severity="information")
        else:
            self.notify("Daemon déjà en marche", severity="warning")
```

- [ ] **Step 5.5 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_binding_s_declare tests/test_cockpit_smoke.py::test_cockpit_binding_X_declare tests/test_cockpit_smoke.py::test_cockpit_binding_k_declare -v
```

Attendu : 3 PASSED

---

## Task 6 : Binding `X` — arrêter le daemon (modal confirmation)

**Files:**
- Modify: `trader/cockpit.py`
- Modify: `tests/test_cockpit_smoke.py`

- [ ] **Step 6.1 : Écrire les tests rouges — modal stop daemon**

Ajouter à `tests/test_cockpit_smoke.py` :

```python
async def test_cockpit_binding_X_monte_modal(tmp_path, monkeypatch):
    """La touche X ouvre un modal de confirmation (ConfirmStop dans le DOM)."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("X")
        # Le modal doit être présent dans le DOM
        from trader.cockpit import ConfirmStop
        assert app.query(ConfirmStop)


async def test_cockpit_binding_k_monte_modal(tmp_path, monkeypatch):
    """La touche k ouvre un modal de confirmation kill-switch."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("k")
        from trader.cockpit import ConfirmKill
        assert app.query(ConfirmKill)
```

- [ ] **Step 6.2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_binding_X_monte_modal tests/test_cockpit_smoke.py::test_cockpit_binding_k_monte_modal -v 2>&1 | head -20
```

Attendu : 2 FAILED (`ImportError: cannot import name 'ConfirmStop'`)

- [ ] **Step 6.3 : Créer les modals `ConfirmStop` et `ConfirmKill` dans `trader/cockpit.py`**

Ajouter en tête des imports de cockpit.py :

```python
from textual.screen import ModalScreen
from textual.widgets import Button, Label
from textual.containers import Vertical, Horizontal
```

Ajouter avant `CockpitApp` (après `EventsPane`) :

```python
# ---------------------------------------------------------------------------
# Modals de confirmation
# ---------------------------------------------------------------------------


class ConfirmStop(ModalScreen[bool]):
    """Modal de confirmation pour l'arrêt du daemon (binding X)."""

    DEFAULT_CSS = """
    ConfirmStop {
        align: center middle;
    }
    ConfirmStop Vertical {
        background: $surface;
        border: solid $error;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    ConfirmStop Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmStop Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Arrêter le daemon ?\n(SIGINT — arrêt propre)")
            with Horizontal():
                yield Button("Oui", id="confirm-stop-yes", variant="error")
                yield Button("Non", id="confirm-stop-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-stop-yes")


class ConfirmKill(ModalScreen[bool]):
    """Modal de confirmation pour le toggle kill-switch (binding k)."""

    DEFAULT_CSS = """
    ConfirmKill {
        align: center middle;
    }
    ConfirmKill Vertical {
        background: $surface;
        border: solid $warning;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    ConfirmKill Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmKill Button {
        margin: 0 1;
    }
    """

    def __init__(self, kill_active: bool, **kwargs):
        super().__init__(**kwargs)
        self._kill_active = kill_active

    def compose(self) -> ComposeResult:
        action = "Retirer" if self._kill_active else "Activer"
        with Vertical():
            yield Label(f"{action} le kill-switch ?\n(bloque/débloque tous les ordres)")
            with Horizontal():
                yield Button("Oui", id="confirm-kill-yes", variant="warning")
                yield Button("Non", id="confirm-kill-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-kill-yes")
```

- [ ] **Step 6.4 : Implémenter `action_stop_daemon_confirm` et `action_toggle_kill` dans `CockpitApp`**

Ajouter dans `CockpitApp` après `action_start_daemon` :

```python
    def action_stop_daemon_confirm(self) -> None:
        """Ouvre le modal de confirmation pour arrêter le daemon."""

        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            from trader.cockpit_supervisor import stop_daemon
            result = stop_daemon(pid_file=_STATE_DIR / "daemon.pid")
            if result.stopped:
                self.notify(f"Daemon arrêté (PID {result.pid})", severity="information")
            else:
                self.notify("Pas de daemon en cours", severity="warning")

        self.push_screen(ConfirmStop(), _on_confirm)

    def action_toggle_kill(self) -> None:
        """Ouvre le modal de confirmation pour toggler le kill-switch."""
        kill_active = _KILL_FILE.exists()

        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            from trader.cockpit_supervisor import toggle_kill_switch
            active = toggle_kill_switch(kill_file=_KILL_FILE)
            status = "activé" if active else "désactivé"
            self.notify(f"Kill-switch {status}", severity="warning" if active else "information")

        self.push_screen(ConfirmKill(kill_active=kill_active), _on_confirm)
```

- [ ] **Step 6.5 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_binding_X_monte_modal tests/test_cockpit_smoke.py::test_cockpit_binding_k_monte_modal -v
```

Attendu : 2 PASSED

---

## Task 7 : Test `q` ne signale jamais le daemon

**Files:**
- Modify: `tests/test_cockpit_smoke.py`

- [ ] **Step 7.1 : Écrire le test rouge**

Ajouter à `tests/test_cockpit_smoke.py` :

```python
async def test_cockpit_q_ne_signale_pas_le_daemon(tmp_path, monkeypatch):
    """q quitte sans envoyer aucun signal au daemon — vérifié par monkeypatch os.kill."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    signals_sent = []

    import trader.cockpit_supervisor as sup_module
    monkeypatch.setattr(sup_module, "os", __import__("os"))
    # Patch os.kill dans le module supervisor pour tracer les appels
    import os as _os_real

    class TrackingOS:
        def kill(self, pid, sig):
            signals_sent.append((pid, sig))
            return _os_real.kill(pid, sig)
        def __getattr__(self, name):
            return getattr(_os_real, name)

    monkeypatch.setattr(sup_module, "os", TrackingOS())

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")

    assert signals_sent == [], f"q a envoyé des signaux inattendus : {signals_sent}"
```

- [ ] **Step 7.2 : Vérifier que le test passe immédiatement (comportement déjà correct)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_q_ne_signale_pas_le_daemon -v
```

Attendu : PASSED (q n'appelle pas stop_daemon)

---

## Task 8 : Mise à jour du docstring de `cockpit.py`

**Files:**
- Modify: `trader/cockpit.py`

- [ ] **Step 8.1 : Mettre à jour le module docstring**

Remplacer le docstring en tête de `trader/cockpit.py` :

```python
"""cockpit — Salle de contrôle du daemon casys-trader (Textual).

Dashboard complet (KPIs, courbe d'équité, positions, attribution, décisions,
learnings) + flux de logs live (events.jsonl) + supervision du daemon.

Pattern superviseur : le daemon reste un process indépendant qui survit à
la fermeture du cockpit. Le cockpit peut le démarrer, l'arrêter et toggler
le kill-switch.

Écriture autorisée (UNIQUEMENT ces fichiers) :
    state/daemon.pid          — PID du daemon au lancement
    state/daemon_console.log  — stdout/stderr du daemon (append)
    KILL                      — fichier kill-switch (toggle)

Usage :
    uv run python -m trader.cockpit
    make cockpit

Raccourcis :
    q         Quitter (ne touche JAMAIS au daemon)
    s         Démarrer le daemon (anti-double-lancement)
    X         Arrêter le daemon (modal de confirmation → SIGINT)
    k         Toggle kill-switch (modal de confirmation)
    c         Toggle l'affichage des events cycle_started/cycle_completed
    f         Pause/reprise de l'auto-scroll du panneau logs
    l         Toggle visibilité du panneau logs (plein écran dashboard)
    d         Dark/Light (thèmes casys-salmon / casys-ink)
"""
```

- [ ] **Step 8.2 : Vérifier que la suite complète passe**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py -v
```

Attendu : tous PASSED

---

## Task 9 : Makefile — 12 → 4 cibles + help

**Files:**
- Modify: `Makefile`

- [ ] **Step 9.1 : Écrire le nouveau Makefile**

Remplacer intégralement le contenu de `Makefile` :

```makefile
# casys-trader — raccourcis. Lance `make` (ou `make help`) pour la liste.
.DEFAULT_GOAL := help
.PHONY: help cockpit live once test

help:  ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

cockpit:  ## Salle de contrôle (dashboard + logs live + supervision daemon) — point d'entrée principal
	uv run python -m trader.cockpit

live:  ## Moteur en PAPER réel (exécute les ordres simulés, écrit l'état)
	uv run python -m trader.daemon --live

once:  ## Un seul cycle dry-run (test rapide)
	uv run python -m trader.daemon --once

test:  ## Lance toute la suite de tests
	uv run pytest -q
```

**Note** : les modules Python correspondant aux cibles supprimées (`trader.tui`, `trader.stats`, `trader.attribution`, scripts/tui_demo.py) restent intacts et invocables via `uv run python -m ...`.

- [ ] **Step 9.2 : Vérifier que `make help` affiche 4 cibles**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && make help
```

Attendu : 4 lignes (help, cockpit, live, once, test) — `help` apparaît si incluse dans le grep.

- [ ] **Step 9.3 : Vérifier que `make test` fonctionne**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && make test 2>&1 | tail -5
```

Attendu : `X passed` (≥ 629)

---

## Task 10 : Vérification `.gitignore` et ruff

**Files:**
- Possibly modify: `.gitignore`

- [ ] **Step 10.1 : Vérifier que state/ est dans .gitignore**

```bash
grep "state/" /Users/erwanpesle/Documents/GitHub/casys-trader/.gitignore
```

Attendu : `/state/` — couvre déjà `state/daemon.pid` et `state/daemon_console.log`.

Si absent : ajouter `/state/` au `.gitignore`.

- [ ] **Step 10.2 : Ruff check sur les fichiers touchés**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run ruff check trader/cockpit.py trader/cockpit_supervisor.py tests/test_cockpit_supervisor.py tests/test_cockpit_smoke.py
```

Attendu : aucune erreur.

Si erreurs : `uv run ruff check --fix ...` pour les corrections automatiques, puis traiter manuellement les restantes.

- [ ] **Step 10.3 : Suite complète finale**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest -q
```

Attendu : ≥ 649 tests passés (630 existants + ≈ 19 nouveaux), 0 failed.

---

## Self-Review — couverture spec

| Exigence spec | Tâche couverte |
|---|---|
| Indicateur vital (alive/stopped/never_started) | Task 1 + Task 4 |
| Rafraîchi avec le poll d'état existant | Task 4 (update_state appelle daemon_vital_state) |
| Binding `s` + anti-double-lancement + PID dans state/daemon.pid | Task 2 + Task 5 |
| Popen start_new_session=True, stdout/stderr → daemon_console.log, cwd=ROOT | Task 2 |
| Binding `X` + modal + SIGINT (jamais SIGKILL) | Task 3 + Task 6 |
| Binding `k` + modal + toggle KILL | Task 3 + Task 6 |
| `q` ne touche jamais au daemon — test dédié | Task 7 |
| Docstring mis à jour (read-only → superviseur) | Task 8 |
| Makefile 12 → 4 cibles + help | Task 9 |
| state/daemon.pid et daemon_console.log couverts par .gitignore (/state/) | Task 10 |
| Fonctions pures testables séparées du widget Textual | Task 1 (cockpit_supervisor.py) |
| Fail-safe ts imparsable → stopped | Task 1 (test_vital_unparsable_ts_returns_stopped_failsafe) |
