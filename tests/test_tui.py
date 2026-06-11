"""tests pour trader.tui — vérifie build_view sans I/O réseau ni boucle live."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from trader.tui import (
    _load_scheduler_data_safe,
    build_view,
    load_runtime_state,
)


# ---------------------------------------------------------------------------
# Fixtures de données
# ---------------------------------------------------------------------------

_FULL_STATE: dict = {
    "ts": "2026-06-05T12:00:00+00:00",
    "dry_run": True,
    "kill_switch": False,
    "portfolio": {
        "cash": 85_000.0,
        "equity": 102_500.0,
        "total_return_pct": 2.5,
        "holdings": [
            {
                "symbol": "AAPL",
                "quantity": 10.0,
                "avg_price": 170.0,
                "last_price": 185.0,
                "unrealized_pnl": 150.0,   # PnL positif → vert
            },
            {
                "symbol": "TSLA",
                "quantity": 5.0,
                "avg_price": 250.0,
                "last_price": 210.0,
                "unrealized_pnl": -200.0,  # PnL négatif → rouge
            },
        ],
    },
    "decisions": [
        {
            "symbol": "AAPL",
            "action": "BUY",
            "qty": 2.0,
            "rationale": "tendance haussière confirmée par momentum 20j",
            "confidence": 0.82,
            "executed": True,
            "reason": "ok",
        },
        {
            "symbol": "TSLA",
            "action": "HOLD",
            "qty": 0.0,
            "rationale": "attente signal de retournement",
            "confidence": 0.55,
            "executed": False,
            "reason": "hold",
        },
    ],
    "daemon_status": {
        "phase": "deciding_symbol",
        "current_symbol": "TSLA",
        "decisions_done": 1,
        "symbols_total": 2,
        "model_calls_used": 3,
        "max_model_calls_per_cycle": 25,
    },
    "source": "current_report",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_view_avec_etat_complet_retourne_un_renderable() -> None:
    """build_view avec un dict complet retourne un RenderableType sans exception."""
    result = build_view(_FULL_STATE)
    # Le résultat doit être un objet affichable par Rich
    assert result is not None
    # Doit être rendu par la console sans planter
    from rich.console import Console
    console = Console(width=120)
    # Tenter d'écrire dans un buffer — si ça ne lève pas, c'est bon
    with console.capture() as capture:
        console.print(result)
    output = capture.get()
    # L'output doit contenir les données clé
    assert "AAPL" in output
    assert "TSLA" in output
    assert "PnL latent total" in output
    assert "-50.00" in output
    assert "deciding_symbol" in output
    assert "1/2" in output
    assert "3/25" in output
    assert "current_report" in output
    assert "dry" in output.lower() or "DRY" in output or "2.5" in output or "BUY" in output


def test_build_view_normalise_le_timestamp_du_cycle_en_utc() -> None:
    state = {
        **_FULL_STATE,
        "ts": "2026-06-05T12:00:00+02:00",
    }

    from rich.console import Console
    console = Console(width=120)
    with console.capture() as capture:
        console.print(build_view(state))
    output = capture.get()

    assert "2026-06-05 10:00 UTC" in output
    assert "2026-06-05T12:00:00+02:00" not in output


def test_build_view_avec_none_retourne_un_renderable() -> None:
    """build_view avec None ne plante pas et retourne quelque chose d'affichable."""
    result = build_view(None)
    assert result is not None
    from rich.console import Console
    console = Console(width=120)
    with console.capture():
        console.print(result)  # ne doit pas lever


def test_build_view_avec_dict_partiel_sans_portfolio_retourne_un_renderable() -> None:
    """build_view avec un dict sans clé 'portfolio' ne plante pas."""
    partial_state: dict = {
        "ts": "2026-06-05T08:00:00+00:00",
        "dry_run": True,
        "decisions": [],
    }
    result = build_view(partial_state)
    assert result is not None
    from rich.console import Console
    console = Console(width=120)
    with console.capture() as capture:
        console.print(result)
    output = capture.get()
    # Pas de positions → doit afficher un placeholder
    assert "—" in output


def test_load_runtime_state_prefere_current_report_et_injecte_status(tmp_path) -> None:
    current = tmp_path / "current_report.json"
    last = tmp_path / "last_report.json"
    status = tmp_path / "daemon_status.json"
    current.write_text('{"ts":"current","portfolio":{"holdings":[]}}', encoding="utf-8")
    last.write_text('{"ts":"last","portfolio":{"holdings":[{"symbol":"OLD"}]}}', encoding="utf-8")
    status.write_text(
        '{"phase":"cycle_completed","decisions_done":5,"symbols_total":5,"model_calls_used":5}',
        encoding="utf-8",
    )

    state = load_runtime_state(
        state_dir=tmp_path,
        current_report_path=current,
        last_report_path=last,
        status_path=status,
    )

    assert state["ts"] == "current"
    assert state["source"] == "current_report"
    assert state["daemon_status"]["phase"] == "cycle_completed"


def test_load_runtime_state_retombe_sur_last_report_si_current_absent(tmp_path) -> None:
    last = tmp_path / "last_report.json"
    last.write_text('{"ts":"last","portfolio":{"holdings":[]}}', encoding="utf-8")

    state = load_runtime_state(
        state_dir=tmp_path,
        current_report_path=tmp_path / "missing_current.json",
        last_report_path=last,
        status_path=tmp_path / "missing_status.json",
    )

    assert state["ts"] == "last"
    assert state["source"] == "last_report"


def test_load_scheduler_data_safe_filtre_les_veilles_expirees(tmp_path) -> None:
    """Le dashboard ne doit pas afficher une veille dont l'échéance est passée.

    Cohérent avec Scheduler.active_indicator_watches côté daemon : le TUI lit
    le fichier en direct, il doit filtrer l'expiration lui-même.
    """
    path = tmp_path / "scheduler.json"
    path.write_text(
        json.dumps(
            {
                "indicator_watches": {
                    "w1": {
                        "id": "w1",
                        "symbol": "EURUSD=X",
                        "expires_at": "2026-06-10T19:40:00+00:00",
                    },
                    "w2": {
                        "id": "w2",
                        "symbol": "USDJPY=X",
                        "expires_at": "2026-06-10T12:00:00+00:00",
                    },
                },
                "stale_streaks": {},
            }
        ),
        encoding="utf-8",
    )

    now = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    watches, _ = _load_scheduler_data_safe(path, now=now)

    assert {w["symbol"] for w in watches} == {"EURUSD=X"}


def test_load_scheduler_data_safe_garde_les_veilles_sans_echeance(tmp_path) -> None:
    """Une veille sans expires_at (ou non parsable) reste visible."""
    path = tmp_path / "scheduler.json"
    path.write_text(
        json.dumps(
            {
                "indicator_watches": {
                    "w1": {"id": "w1", "symbol": "SPY"},
                },
                "stale_streaks": {},
            }
        ),
        encoding="utf-8",
    )

    now = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    watches, _ = _load_scheduler_data_safe(path, now=now)

    assert {w["symbol"] for w in watches} == {"SPY"}
