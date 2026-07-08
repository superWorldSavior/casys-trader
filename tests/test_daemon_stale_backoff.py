"""Tests chantier 1 : backoff stale et déduplication décisions."""
import json
from datetime import datetime, timezone


from trader.agent.client import Decision
from trader.runtime import daemon
from trader.runtime import cycle_scheduling
from trader.planning.scheduler import Scheduler, STALE_BACKOFF_MAX_MINUTES, STALE_BACKOFF_MAX_STREAK


def test_backoff_wake_premier_stale_est_egal_au_defaut(tmp_path) -> None:
    """streak=0 → next_wake = default (pas de doublement au premier stale)."""
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=0, default_wake_minutes=30.0)
    assert result == 30.0


def test_backoff_wake_doublee_au_second_stale(tmp_path) -> None:
    """streak=1 → 30 * 2^1 = 60."""
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=1, default_wake_minutes=30.0)
    assert result == 60.0


def test_backoff_wake_triple_stale(tmp_path) -> None:
    """streak=2 → 30 * 2^2 = 120."""
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=2, default_wake_minutes=30.0)
    assert result == 120.0


def test_backoff_wake_cap_a_120_minutes(tmp_path) -> None:
    """streak=10 → capp à STALE_BACKOFF_MAX_MINUTES=120."""
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=10, default_wake_minutes=30.0)
    assert result == STALE_BACKOFF_MAX_MINUTES


def test_backoff_wake_cap_avec_defaut_petit(tmp_path) -> None:
    """Avec default=5min, streak=5 → 5*32=160 → cappé à 120."""
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=5, default_wake_minutes=5.0)
    assert result == STALE_BACKOFF_MAX_MINUTES


# ── helpers communs ──────────────────────────────────────────────────────────

def _write_runtime_config(root) -> None:
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n"
    )
    (root / "config" / "risk.yaml").write_text(
        "\n".join([
            "max_position_value: 20000",
            "max_gross_exposure: 100000",
            "max_order_value: 10000",
            "min_equity: 50000",
        ])
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _make_stale_source(now, age_minutes=90.0):
    """Retourne une data source TOUT stale : runtime trop vieux ET daily d'une séance
    ancienne (§13.4 : un daily du JOUR serait jugé frais par séance → analysable ;
    pour rester dans le cas "rien d'exploitable → HOLD + backoff", le daily doit
    couvrir une séance révolue)."""
    from datetime import timedelta
    from trader.market.market_data import Bar

    class FakeStaleDataSource:
        def get_bars(self, symbol, lookback, interval):
            if interval == "1d":
                old = (now - timedelta(days=10)).date().isoformat()
                return [Bar(ts=old, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]
            stale_ts = (now - timedelta(minutes=age_minutes)).isoformat()
            return [Bar(ts=stale_ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]

    return FakeStaleDataSource()


def _hold_decision(**kwargs):
    return Decision.hold(kwargs["symbol"], "hold")


# ── tests intégration ────────────────────────────────────────────────────────

def test_stale_premier_cycle_wake_egal_au_defaut(monkeypatch, tmp_path, patch_batch) -> None:
    """Premier stale (streak=0) → next_wake = default_wake_minutes."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    # Streak après premier stale = 1 (on vient de staler)
    assert sched.get_stale_streak("SPY") == 1
    # Wake = default (30min), pas encore de backoff
    wake = sched.next_wake("SPY")
    delta = abs((wake - now).total_seconds())
    assert abs(delta - 30 * 60) < 2, f"wake delta={delta}s attendu=1800s"


def test_stale_deuxieme_cycle_wake_double(monkeypatch, tmp_path, patch_batch) -> None:
    """Deuxième stale consécutif (streak=1) → next_wake = default * 2."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 1)  # simuler: 1er stale déjà arrivé
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    assert sched.get_stale_streak("SPY") == 2
    wake = sched.next_wake("SPY")
    delta = abs((wake - now).total_seconds())
    assert abs(delta - 60 * 60) < 2, f"wake delta={delta}s attendu=3600s (60 min)"


def test_stale_streak_cappe_a_120_minutes(monkeypatch, tmp_path, patch_batch) -> None:
    """Streak=10 → wake cappé à 120min."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 10)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    wake = sched.next_wake("SPY")
    delta = (wake - now).total_seconds()
    assert abs(delta - 120 * 60) < 2, f"wake delta={delta}s attendu=7200s (120 min cappé)"


def test_stale_streak_reset_quand_data_fraiche(monkeypatch, tmp_path, patch_batch) -> None:
    """Après une donnée fraîche, le streak est remis à 0."""
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 3)  # streak existant
    now = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)  # heure de marché

    class FreshDataSource:
        def get_bars(self, symbol, lookback, interval):
            # barre très récente (1 minute old)
            from datetime import timedelta
            fresh_ts = (now - timedelta(minutes=1)).isoformat()
            return [Bar(ts=fresh_ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]

    patch_batch(_hold_decision)

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=FreshDataSource(),
        default_wake_minutes=30.0,
        max_market_data_age_minutes=5.0,
    )

    assert sched.get_stale_streak("SPY") == 0


def test_stale_premiere_occurrence_enregistre_une_decision(monkeypatch, tmp_path, patch_batch) -> None:
    """Premier stale (streak=0) → 1 décision HOLD dans report['decisions']."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    decisions = report["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["reason"] == "stale_market_data"
    assert decisions[0]["stale_streak"] == 1


def test_stale_occurrences_suivantes_ne_creent_pas_de_decision(monkeypatch, tmp_path, patch_batch) -> None:
    """Stales suivants (streak>=1) → 0 décision dans report, mais event stale_backoff dans events.jsonl."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 2)  # déjà 2 stales, pas le premier
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    # Pas de décision enregistrée pour ce cycle
    assert report["decisions"] == []

    # Event léger dans events.jsonl
    events_path = state_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    stale_events = [e for e in events if e.get("event") == "stale_backoff"]
    assert len(stale_events) >= 1
    ev = stale_events[-1]
    assert ev["symbol"] == "SPY"
    assert ev["streak"] == 3
    assert "next_wake_minutes" in ev
    assert "stale_reason" in ev


def test_stale_sans_scheduler_ne_plante_pas(monkeypatch, tmp_path, patch_batch) -> None:
    """run_cycle sans sched=None ne doit pas planter sur stale."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    # Pas de sched → doit fonctionner sans crash
    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=None, data_source=data_source,
        default_wake_minutes=30.0,
    )

    # Premier stale sans sched → 1 décision quand même
    assert len(report["decisions"]) == 1
    assert report["decisions"][0]["reason"] == "stale_market_data"


# ── MAJOR 1 : cap appliqué même sur streak=0 ────────────────────────────────

def test_backoff_wake_default_superieur_au_cap_est_ramene_au_cap(tmp_path) -> None:
    """streak=0, default=240 → cappé à STALE_BACKOFF_MAX_MINUTES (120), pas 240."""
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=0, default_wake_minutes=240.0)
    assert result == STALE_BACKOFF_MAX_MINUTES, (
        f"Attendu {STALE_BACKOFF_MAX_MINUTES}, obtenu {result} — le cap doit s'appliquer même à streak=0"
    )


def test_stale_premier_cycle_default_240_wake_cappee_a_120(monkeypatch, tmp_path, patch_batch) -> None:
    """Intégration : default_wake_minutes=240, premier stale → next_wake = 120min (cap)."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=240.0,
    )

    wake = sched.next_wake("SPY")
    delta = (wake - now).total_seconds()
    assert abs(delta - 120 * 60) < 2, (
        f"Attendu 7200s (120min cappé), obtenu {delta}s — default=240 doit être cappé à 120"
    )


# ── MAJOR 2 : streak persisté borné, pas d'overflow ─────────────────────────

def test_backoff_streak_max_constant_exported() -> None:
    """STALE_BACKOFF_MAX_STREAK doit être exporté depuis scheduler."""
    assert STALE_BACKOFF_MAX_STREAK > 0
    assert STALE_BACKOFF_MAX_STREAK <= 20  # raisonnable


def test_backoff_wake_streak_enorme_pas_overflow() -> None:
    """Un streak arbitrairement grand (ex. 9999) ne provoque pas d'OverflowError."""
    # Prouvé au REPL : 30 * 2**9999 → OverflowError sur float
    result = cycle_scheduling.stale_backoff_wake_minutes(streak=9999, default_wake_minutes=30.0)
    assert result == STALE_BACKOFF_MAX_MINUTES


def test_streak_persiste_borne_au_max(tmp_path) -> None:
    """set_stale_streak borne la valeur à STALE_BACKOFF_MAX_STREAK."""
    sched = Scheduler(tmp_path / "scheduler.json")
    # Simuler un state corrompu avec un streak astronomique
    sched.set_stale_streak("SPY", 9999)
    assert sched.get_stale_streak("SPY") == STALE_BACKOFF_MAX_STREAK


def test_streak_persistance_apres_reload_borne(tmp_path) -> None:
    """Après rechargement depuis disque, le streak est toujours borné."""
    path = tmp_path / "scheduler.json"
    sched1 = Scheduler(path)
    sched1.set_stale_streak("SPY", 9999)
    sched2 = Scheduler(path)
    assert sched2.get_stale_streak("SPY") == STALE_BACKOFF_MAX_STREAK


def test_streak_vieux_state_corrompu_ne_crashe_pas(tmp_path) -> None:
    """Un scheduler.json avec un streak énorme (state corrompu) → pas de crash, wake=cap."""
    path = tmp_path / "scheduler.json"
    # Écrire directement un state avec streak corrompu
    path.write_text('{"default_next_wake": null, "symbols": {}, "indicator_watches": {}, "stale_streaks": {"SPY": 9999}}')
    sched = Scheduler(path)
    # get_stale_streak doit borner à la lecture
    streak = sched.get_stale_streak("SPY")
    assert streak == STALE_BACKOFF_MAX_STREAK
    # Et le calcul de wake ne doit pas overflow
    wake = cycle_scheduling.stale_backoff_wake_minutes(streak=streak, default_wake_minutes=30.0)
    assert wake == STALE_BACKOFF_MAX_MINUTES


# ── MINOR 3 : setup_logging couvre trader.* ─────────────────────────────────

def test_setup_logging_couvre_les_loggers_trader() -> None:
    """Un log émis par trader.market.data_source ressort via le handler installé."""
    import io
    import logging
    from trader.runtime.logging_setup import setup_logging

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    # Utiliser un StringIO capturant pour le handler
    out = io.StringIO()
    out.isatty = lambda: False  # type: ignore[attr-defined]
    setup_logging(level=logging.DEBUG, stream=out)

    # Émettre un log depuis un module trader.* autre que casys-trader
    sub_logger = logging.getLogger("trader.market.data_source")
    sub_logger.info("source_fallback test_message_unique_xyz")

    output = out.getvalue()
    assert "test_message_unique_xyz" in output, (
        f"Le log de trader.market.data_source doit être capturé. Output: {repr(output)}"
    )


# ── MINOR 4 : tests complémentaires ─────────────────────────────────────────

def test_dedup_apres_restart_nouveau_scheduler(monkeypatch, tmp_path, patch_batch) -> None:
    """Après redémarrage (nouveau Scheduler sur le même fichier), le streak persiste
    et le deuxième stale consécutif ne crée PAS de nouvelle décision."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    scheduler_path = state_dir / "scheduler.json"
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(_hold_decision)

    # Cycle 1 : premier stale → streak=1, 1 décision enregistrée
    sched1 = Scheduler(scheduler_path)
    report1 = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched1, data_source=data_source,
        default_wake_minutes=30.0,
    )
    assert len(report1["decisions"]) == 1
    assert sched1.get_stale_streak("SPY") == 1

    # Simuler redémarrage : nouveau Scheduler sur même fichier
    sched2 = Scheduler(scheduler_path)
    assert sched2.get_stale_streak("SPY") == 1  # streak persisté

    # Cycle 2 : même data stale → streak=2, 0 décision (event léger seulement)
    report2 = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched2, data_source=data_source,
        default_wake_minutes=30.0,
    )
    assert report2["decisions"] == [], "Après redémarrage, le deuxième stale ne doit pas créer de décision"
    assert sched2.get_stale_streak("SPY") == 2


def test_indicator_watch_trigger_reset_streak(monkeypatch, tmp_path, patch_batch) -> None:
    """Après un backoff stale, un trigger indicator_watch réveille le symbole ;
    la data fraîche qui suit remet le streak à 0."""
    from datetime import timedelta
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 3)  # streak en cours de backoff
    now = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)

    class FreshDataSource:
        def get_bars(self, symbol, lookback, interval):
            fresh_ts = (now - timedelta(minutes=1)).isoformat()
            return [Bar(ts=fresh_ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]

    patch_batch(_hold_decision)

    # Simuler un indicator trigger qui a réveillé le symbole
    trigger = {
        "watch_id": "spy-watch",
        "symbol": "SPY",
        "on_trigger": "WAKE",
        "matched": [{"indicator": "return", "actual": 0.1, "op": ">", "value": 0.05}],
    }

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=FreshDataSource(),
        default_wake_minutes=30.0,
        max_market_data_age_minutes=5.0,
        indicator_triggers=[trigger],
    )

    # Data fraîche → streak remis à 0
    assert sched.get_stale_streak("SPY") == 0, (
        "Après trigger indicator_watch + data fraîche, le streak doit être 0"
    )


def test_run_cycle_nappelle_pas_le_llm_sur_un_stale_sans_prix(
    monkeypatch, tmp_path, patch_batch
) -> None:
    """§13.4 — un symbole daily-valide MAIS sans aucune barre runtime (donc sans prix)
    ne doit PAS coûter un appel LLM perdu : il n'entre pas dans decidable, il retombe
    sur le HOLD stale. Pas de décision fantôme (anti gap silencieux)."""
    from trader.agent.client import Decision
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

    class NoRuntimeFreshDaily:
        def get_bars(self, symbol, lookback, interval):
            if interval == "1d":
                return [
                    Bar(ts=d, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
                    for d in ("2026-06-11", "2026-06-12", "2026-06-15")
                ]
            return []  # aucune barre runtime → pas de prix

    calls: list[str] = []

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "ne doit pas être appelé")

    patch_batch(decide)

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=NoRuntimeFreshDaily(),
    )

    assert "SPY" not in calls  # pas d'appel LLM perdu (le symbole n'entre pas dans le batch)
    assert report["model_calls_used"] == 0


def test_run_cycle_appelle_le_llm_sur_stale_avec_daily_valide(
    monkeypatch, tmp_path, patch_batch
) -> None:
    """§13.4 — incident Taïwan : runtime stale MAIS daily valide → le LLM est appelé
    (analyse swing, execution.enabled=false), au lieu d'un HOLD synthétique infra."""
    from datetime import timedelta

    from trader.agent.client import Decision
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)  # lundi 10:30 ET

    class StaleRuntimeFreshDaily:
        def get_bars(self, symbol, lookback, interval):
            if interval == "1d":
                # daily valide : couvre la dernière séance complétée (vendredi 12)
                return [
                    Bar(ts=d, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
                    for d in ("2026-06-11", "2026-06-12", "2026-06-15")
                ]
            # runtime stale : barres 15m vieilles de 3h (> budget de fraîcheur)
            ts = (now - timedelta(hours=3)).isoformat()
            return [Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0) for _ in range(32)]

    calls: list[str] = []

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        return Decision(
            symbol=kwargs["symbol"], action="HOLD", quantity=0.0, confidence=0.5,
            rationale="analyse swing", intent="HOLD", llm_provider="acpx", llm_model="gpt-5.5/medium",
        )

    patch_batch(decide)

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=StaleRuntimeFreshDaily(),
    )

    # Le LLM a bien été appelé malgré le runtime stale (plus de HOLD synthétique aveugle).
    assert "SPY" in calls
    dec = report["decisions"][0]
    assert dec["decision_source"] == "llm"
    assert dec["reason"] != "stale_market_data"


def test_run_cycle_fetch_le_daily_meme_pour_un_symbole_runtime_stale(
    monkeypatch, tmp_path, patch_batch
) -> None:
    """§13.3 — le daily/swing context doit être fetché même quand le runtime est
    stale, pour permettre l'analyse swing hors marché. Avant : la boucle daily ne
    couvrait que tradable_symbols (= non-stale), donc un stale n'avait jamais de daily."""
    from datetime import timedelta

    from trader.agent.client import Decision
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    requests: list[tuple[str, str, str]] = []

    class StaleRuntimeSource:
        def get_bars(self, symbol, lookback, interval):
            requests.append((symbol, lookback, interval))
            # Barre vieille de 90 min → runtime stale pour tous les intervalles.
            ts = (now - timedelta(minutes=90)).isoformat()
            return [Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]

    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=StaleRuntimeSource(),
    )

    # Le daily est demandé pour SPY MÊME s'il est runtime-stale.
    assert (
        "SPY",
        daemon.COCKPIT_DAILY_LOOKBACK,
        daemon.COCKPIT_DAILY_INTERVAL,
    ) in requests


def test_run_cycle_bloque_l_ordre_hors_session_meme_avec_donnees_fraiches(
    monkeypatch, tmp_path, patch_batch
) -> None:
    """§13.5 — données runtime FRAÎCHES mais session fermée (samedi) →
    execution.enabled=false → l'ordre LLM ne part pas (executed=False,
    reason=execution:session_closed). La garde précède le RiskGate."""
    from datetime import timedelta

    from trader.agent.client import Decision
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)  # samedi → session US fermée

    class FreshButClosedSource:
        def get_bars(self, symbol, lookback, interval):
            ts = (now - timedelta(minutes=5)).isoformat()  # frais → non stale
            return [
                Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
                for _ in range(32)
            ]

    patch_batch(
        lambda **kwargs: Decision(
            symbol=kwargs["symbol"], action="BUY", quantity=10.0, confidence=0.9,
            rationale="open", intent="OPEN_LONG", llm_provider="acpx", llm_model="gpt-5.5/medium",
        )
    )

    report = daemon.run_cycle(
        dry_run=False, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=FreshButClosedSource(),
    )

    dec = report["decisions"][0]
    assert dec["executed"] is False
    assert dec["reason"] == "execution:session_closed"


def test_run_cycle_ordre_bloque_hors_session_conserve_le_wake_du_llm(
    monkeypatch, tmp_path, patch_batch
) -> None:
    """§13.5 — quand l'ordre est bloqué (session fermée), la veille / le réveil posés
    par le LLM sont CONSERVÉS (le LLM a pu planifier en plus de proposer l'ordre)."""
    from datetime import timedelta

    from trader.agent.client import Decision
    from trader.market.market_data import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)  # samedi → session fermée

    class FreshButClosedSource:
        def get_bars(self, symbol, lookback, interval):
            ts = (now - timedelta(minutes=5)).isoformat()
            return [
                Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
                for _ in range(32)
            ]

    patch_batch(
        lambda **kwargs: Decision(
            symbol=kwargs["symbol"], action="BUY", quantity=10.0, confidence=0.9,
            rationale="open", intent="OPEN_LONG", next_wake_in_minutes=90.0,
            llm_provider="acpx", llm_model="gpt-5.5/medium",
        )
    )

    daemon.run_cycle(
        dry_run=False, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=FreshButClosedSource(),
        default_wake_minutes=30.0,  # distinct du wake LLM (90) pour discriminer
    )

    # Le réveil demandé par le LLM (90 min) n'est PAS perdu ni remplacé par le défaut.
    wake = sched.next_wake("SPY")
    assert wake is not None
    assert abs((wake - now).total_seconds() - 90 * 60) < 2
