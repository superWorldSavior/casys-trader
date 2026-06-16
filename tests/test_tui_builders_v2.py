"""TDD pour les 4 nouveaux builders cockpit v2 et tokens palette associés."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from trader.palette import ALL_PALETTE_KEYS, PALETTE_DARK, PALETTE_LIGHT


def _render(renderable, width: int = 120) -> str:
    console = Console(width=width, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ---------------------------------------------------------------------------
# Task 1 — tokens palette
# ---------------------------------------------------------------------------


def test_palette_contient_border_plans() -> None:
    """Les deux palettes exposent le token border_plans."""
    assert "border_plans" in PALETTE_DARK
    assert "border_plans" in PALETTE_LIGHT


def test_palette_contient_border_watches() -> None:
    assert "border_watches" in PALETTE_DARK
    assert "border_watches" in PALETTE_LIGHT


def test_palette_contient_border_llm_activity() -> None:
    assert "border_llm_activity" in PALETTE_DARK
    assert "border_llm_activity" in PALETTE_LIGHT


def test_all_palette_keys_inclut_nouveaux_tokens() -> None:
    for key in ("border_plans", "border_watches", "border_llm_activity"):
        assert key in ALL_PALETTE_KEYS


def test_palette_light_ne_contient_pas_couleurs_dark_brutes_apres_ajout() -> None:
    """PALETTE_LIGHT ne doit pas contenir de valeurs DARK brutes pour les nouveaux tokens."""
    dark_raw = {
        "cyan",
        "magenta",
        "blue",
        "green",
        "red",
        "bold green",
        "bold red",
        "bold cyan",
        "dim white",
        "dim grey50",
        "bold yellow",
    }
    for key in ("border_plans", "border_watches", "border_llm_activity"):
        assert PALETTE_LIGHT[key] not in dark_raw, (
            f"PALETTE_LIGHT['{key}'] = {PALETTE_LIGHT[key]!r} est une couleur DARK brute"
        )


# ---------------------------------------------------------------------------
# Task 2 — reader _load_trade_plans_safe
# ---------------------------------------------------------------------------


def test_load_trade_plans_safe_retourne_liste_avec_fichier_valide(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe

    plans_file = tmp_path / "trade_plans.json"
    plans_file.write_text(
        '{"plans": [{"id": "AAPL-2026", "symbol": "AAPL", "side": "LONG",'
        '"entry_price": 170.0, "remaining_quantity": 5.0, "opened_at": "2026-06-10T10:00:00Z",'
        '"hard_stop_price": 160.0, "take_profits": [{"name": "tp1", "price": 185.0, "fraction": 0.5, "quantity": 2.5}],'
        '"max_hold_minutes": 240.0, "quantity": 5.0}]}',
        encoding="utf-8",
    )
    plans = _load_trade_plans_safe(plans_file)
    assert isinstance(plans, list)
    assert len(plans) == 1
    assert plans[0]["symbol"] == "AAPL"


def test_load_trade_plans_safe_retourne_vide_si_fichier_absent(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe

    result = _load_trade_plans_safe(tmp_path / "nope.json")
    assert result == []


def test_load_trade_plans_safe_retourne_vide_si_json_corrompu(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe

    bad = tmp_path / "trade_plans.json"
    bad.write_text("{not valid json", encoding="utf-8")
    result = _load_trade_plans_safe(bad)
    assert result == []


def test_load_trade_plans_safe_retourne_vide_si_plans_absent(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe

    f = tmp_path / "trade_plans.json"
    f.write_text('{"plans": []}', encoding="utf-8")
    result = _load_trade_plans_safe(f)
    assert result == []


# ---------------------------------------------------------------------------
# Task 2 — builder _build_exit_plans_panel
# ---------------------------------------------------------------------------


def test_build_exit_plans_panel_avec_un_plan_contient_symbol() -> None:
    from trader.tui import _build_exit_plans_panel

    plans = [
        {
            "id": "AAPL-2026",
            "symbol": "AAPL",
            "side": "LONG",
            "entry_price": 170.0,
            "remaining_quantity": 5.0,
            "opened_at": "2026-06-10T10:00:00Z",
            "hard_stop_price": 160.0,
            "take_profits": [
                {"name": "tp1", "price": 185.0, "fraction": 0.5, "quantity": 2.5}
            ],
            "max_hold_minutes": 240.0,
            "quantity": 5.0,
        }
    ]
    result = _build_exit_plans_panel(plans)
    output = _render(result)
    assert "AAPL" in output


def test_build_exit_plans_panel_avec_plan_affiche_stop_distance() -> None:
    from trader.tui import _build_exit_plans_panel

    plans = [
        {
            "id": "AAPL-2026",
            "symbol": "AAPL",
            "side": "LONG",
            "entry_price": 200.0,
            "remaining_quantity": 5.0,
            "opened_at": "2026-06-10T10:00:00Z",
            "hard_stop_price": 180.0,
            "take_profits": [],
            "max_hold_minutes": None,
            "quantity": 5.0,
        }
    ]
    result = _build_exit_plans_panel(plans)
    output = _render(result)
    # stop distance = (200-180)/200 = 10%
    assert "10.0" in output or "10%" in output or "180" in output


def test_build_exit_plans_panel_vide_affiche_aucun_plan() -> None:
    from trader.tui import _build_exit_plans_panel

    result = _build_exit_plans_panel([])
    output = _render(result)
    assert "aucun" in output.lower()


def test_build_exit_plans_panel_avec_light_utilise_palette_light() -> None:
    from trader.tui import _build_exit_plans_panel

    result = _build_exit_plans_panel([], palette=PALETTE_LIGHT)
    # La border_style doit refléter la palette light (border_plans = #0D7680)
    assert result.border_style == PALETTE_LIGHT["border_plans"]
    output = _render(result)
    assert "aucun" in output.lower()


def test_build_exit_plans_panel_retourne_panel() -> None:
    from trader.tui import _build_exit_plans_panel

    result = _build_exit_plans_panel([])
    assert isinstance(result, Panel)
    # Titre exposé
    assert result.title is not None


def test_build_exit_plans_panel_absent_ne_crashe_pas() -> None:
    """Simule fichier absent : plans=[] → Panel avec titre 'Plans sortie'."""
    from trader.tui import _build_exit_plans_panel

    result = _build_exit_plans_panel([])
    assert isinstance(result, Panel)
    # Le titre doit contenir "Plan" (insensible à la casse une fois rendu)
    title_rendered = _render(result.title) if result.title else ""
    assert "plan" in title_rendered.lower() or "Plan" in str(result.title)


# ---------------------------------------------------------------------------
# Task 3 — reader _load_indicator_watches_safe
# ---------------------------------------------------------------------------


def test_load_indicator_watches_safe_retourne_liste_avec_fichier_valide(tmp_path) -> None:
    from trader.tui import _load_indicator_watches_safe

    sched = {
        "indicator_watches": {
            "AAPL:abc123": {
                "id": "AAPL:abc123",
                "symbol": "AAPL",
                "created_at": "2026-06-10T01:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "logic": "any",
                "conditions": [
                    {
                        "indicator": "z_score",
                        "op": "abs>=",
                        "value": 2.0,
                        "timeframe": "1h",
                    }
                ],
            }
        },
        "stale_streaks": {},
    }
    f = tmp_path / "scheduler.json"
    import json as _json

    f.write_text(_json.dumps(sched), encoding="utf-8")
    watches = _load_indicator_watches_safe(f)
    assert isinstance(watches, list)
    assert len(watches) == 1
    assert watches[0]["symbol"] == "AAPL"


def test_load_indicator_watches_safe_retourne_vide_si_absent(tmp_path) -> None:
    from trader.tui import _load_indicator_watches_safe

    result = _load_indicator_watches_safe(tmp_path / "nope.json")
    assert result == []


def test_load_indicator_watches_safe_retourne_vide_si_corrompu(tmp_path) -> None:
    from trader.tui import _load_indicator_watches_safe

    bad = tmp_path / "scheduler.json"
    bad.write_text("{bad json", encoding="utf-8")
    result = _load_indicator_watches_safe(bad)
    assert result == []


def test_load_indicator_watches_safe_charge_stale_streaks(tmp_path) -> None:
    """La fonction doit retourner les stale_streaks via le second return value."""
    from trader.tui import _load_scheduler_data_safe

    import json as _json

    sched = {"indicator_watches": {}, "stale_streaks": {"SPY": 3}}
    f = tmp_path / "scheduler.json"
    f.write_text(_json.dumps(sched), encoding="utf-8")
    watches, streaks = _load_scheduler_data_safe(f)
    assert streaks == {"SPY": 3}


# ---------------------------------------------------------------------------
# Task 3 — builder _build_watches_panel
# ---------------------------------------------------------------------------


def test_build_watches_panel_avec_watches_contient_symbol() -> None:
    from trader.tui import _build_watches_panel

    watches = [
        {
            "id": "AAPL:abc",
            "symbol": "AAPL",
            "expires_at": "2026-06-10T14:00:00Z",
            "logic": "any",
            "conditions": [
                {"indicator": "z_score", "op": "abs>=", "value": 2.0, "timeframe": "1h"}
            ],
        }
    ]
    result = _build_watches_panel(watches)
    output = _render(result)
    assert "AAPL" in output


def test_build_watches_panel_affiche_condition_compacte() -> None:
    from trader.tui import _build_watches_panel

    watches = [
        {
            "id": "X:1",
            "symbol": "X",
            "expires_at": "2026-06-10T12:00:00Z",
            "logic": "any",
            "conditions": [
                {"indicator": "rsi", "op": ">=", "value": 70.0, "timeframe": "4h"}
            ],
        }
    ]
    output = _render(_build_watches_panel(watches))
    # La condition doit être présente sous forme compacte
    assert "rsi" in output
    assert "70" in output


def test_build_watches_panel_vide_affiche_aucune_veille() -> None:
    from trader.tui import _build_watches_panel

    result = _build_watches_panel([])
    output = _render(result)
    assert "aucune" in output.lower()


def test_build_watches_panel_avec_light_utilise_palette_light() -> None:
    from trader.tui import _build_watches_panel

    result = _build_watches_panel([], palette=PALETTE_LIGHT)
    # border_style doit correspondre au token border_watches de PALETTE_LIGHT
    assert result.border_style == PALETTE_LIGHT["border_watches"]
    output = _render(result)
    assert "aucune" in output.lower()


def test_build_watches_panel_retourne_panel() -> None:
    from trader.tui import _build_watches_panel

    result = _build_watches_panel([])
    assert isinstance(result, Panel)
    assert result.title is not None


# ---------------------------------------------------------------------------
# Task 4 — reader _tail_decisions_safe (borné)
# ---------------------------------------------------------------------------


def test_tail_decisions_safe_lit_les_n_dernieres_lignes(tmp_path) -> None:
    import json as _json

    from trader.tui import _tail_decisions_safe

    f = tmp_path / "decisions.jsonl"
    rows = [{"symbol": f"SYM{i}", "action": "HOLD", "runtime": {}} for i in range(100)]
    f.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    result = _tail_decisions_safe(f, n=10)
    assert len(result) == 10
    # Les 10 dernières → SYM90 à SYM99
    symbols = [r["symbol"] for r in result]
    assert "SYM99" in symbols
    assert "SYM0" not in symbols


def test_tail_decisions_safe_gros_fichier_lit_n_lignes(tmp_path) -> None:
    """500 lignes → seules 50 dernières lues, I/O bornée (pas de read_text complet)."""
    import json as _json

    from trader.tui import _tail_decisions_safe

    f = tmp_path / "decisions.jsonl"
    rows = [
        {"symbol": f"S{i}", "runtime": {"data_source": f"src{i}"}} for i in range(500)
    ]
    f.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")

    result = _tail_decisions_safe(f, n=50)
    assert len(result) == 50
    assert result[-1]["symbol"] == "S499"
    assert result[0]["symbol"] == "S450"


def test_tail_decisions_safe_gros_fichier_io_bornee(tmp_path, monkeypatch) -> None:
    """L'algorithme tail doit lire << fichier complet (chunks depuis la fin).

    Vérifie que les octets effectivement lus via fh.read() sont inférieurs
    à 50 % de la taille du fichier — garantit qu'on ne charge pas tout en mémoire.
    """
    import builtins
    import json as _json

    from trader.tui import _tail_decisions_safe

    f = tmp_path / "decisions.jsonl"
    rows = [
        {"symbol": f"S{i}", "runtime": {"data_source": f"src{i}"}} for i in range(500)
    ]
    f.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    total_size = f.stat().st_size

    bytes_read: list[int] = []
    _real_open = builtins.open

    class _TrackingFile:
        """Wrappeur minimal autour du fichier binaire pour compter les read()."""

        def __init__(self, fh):  # type: ignore[no-untyped-def]
            self._fh = fh

        def seek(self, pos: int) -> int:
            return self._fh.seek(pos)

        def read(self, n: int = -1) -> bytes:
            data = self._fh.read(n)
            bytes_read.append(len(data))
            return data

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

    _orig_open = builtins.open

    def _patched_open(path, mode="r", **kwargs):  # type: ignore[no-untyped-def]
        fh = _orig_open(path, mode, **kwargs)
        if "b" in str(mode) and str(path) == str(f):
            return _TrackingFile(fh)
        return fh

    monkeypatch.setattr(builtins, "open", _patched_open)

    result = _tail_decisions_safe(f, n=50)
    assert len(result) == 50

    total_bytes_read = sum(bytes_read)
    # L'algorithme tail lit depuis la fin par chunks de 8192 octets.
    # 50 lignes de ~60 octets chacune ≈ 3000 octets → 1-2 chunks max.
    # On vérifie qu'on n'a pas lu plus de 50 % du fichier total.
    assert total_bytes_read < total_size // 2, (
        f"Trop d'octets lus ({total_bytes_read} / {total_size}) — "
        "l'algorithme ne tail pas correctement"
    )


def test_tail_decisions_safe_fichier_absent_retourne_vide(tmp_path) -> None:
    from trader.tui import _tail_decisions_safe

    result = _tail_decisions_safe(tmp_path / "nope.jsonl")
    assert result == []


def test_tail_decisions_safe_lignes_corrompues_ignorees(tmp_path) -> None:
    import json as _json

    from trader.tui import _tail_decisions_safe

    f = tmp_path / "decisions.jsonl"
    lines = [
        _json.dumps({"symbol": "A", "runtime": {}}),
        "not json",
        _json.dumps({"symbol": "B", "runtime": {"data_source": "live"}}),
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    result = _tail_decisions_safe(f)
    symbols = [r["symbol"] for r in result]
    assert "A" in symbols
    assert "B" in symbols
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Task 4 — builder _build_data_health_panel
# ---------------------------------------------------------------------------


def test_build_data_health_panel_avec_streaks_affiche_symbole() -> None:
    from trader.tui import _build_data_health_panel

    stale_streaks = {"SPY": 3, "QQQ": 1}
    recent_decisions = [
        {"symbol": "SPY", "runtime": {"data_source": "backfill"}},
        {"symbol": "QQQ", "runtime": {"data_source": "live"}},
    ]
    result = _build_data_health_panel(recent_decisions, stale_streaks)
    output = _render(result)
    assert "SPY" in output


def test_build_data_health_panel_streak_positif_affiche_backoff() -> None:
    from trader.tui import _build_data_health_panel

    stale_streaks = {"AAPL": 2}
    recent_decisions = [{"symbol": "AAPL", "runtime": {"data_source": "backfill"}}]
    result = _build_data_health_panel(recent_decisions, stale_streaks)
    output = _render(result)
    # Doit mentionner le backoff
    assert "backoff" in output.lower() or "×2" in output or "2" in output


def test_build_data_health_panel_vide_retourne_panel() -> None:
    from trader.tui import _build_data_health_panel

    result = _build_data_health_panel([], {})
    assert isinstance(result, Panel)


def test_build_data_health_panel_avec_light_utilise_palette_light() -> None:
    from trader.tui import _build_data_health_panel

    result = _build_data_health_panel([], {}, palette=PALETTE_LIGHT)
    # border_style doit correspondre au token border_default de PALETTE_LIGHT
    assert result.border_style == PALETTE_LIGHT["border_default"]
    output = _render(result)
    assert len(output.strip()) > 0


# ---------------------------------------------------------------------------
# Task 5 — builder _build_llm_activity_panel
# ---------------------------------------------------------------------------


def test_build_llm_activity_panel_affiche_appels_modele() -> None:
    from trader.tui import _build_llm_activity_panel

    daemon_status = {"model_calls_used": 5, "max_model_calls_per_cycle": 25}
    result = _build_llm_activity_panel(daemon_status, 12, None)
    output = _render(result)
    assert "5" in output
    assert "25" in output


def test_build_llm_activity_panel_affiche_learnings_en_attente() -> None:
    from trader.tui import _build_llm_activity_panel

    result = _build_llm_activity_panel({}, 42, None)
    output = _render(result)
    assert "42" in output


def test_build_llm_activity_panel_affiche_consolidation_status() -> None:
    from trader.tui import _build_llm_activity_panel

    consolidation = {"status": "success", "ts": "2026-06-10T05:00:00Z", "count": 3}
    result = _build_llm_activity_panel({}, 0, consolidation)
    output = _render(result)
    assert "success" in output or "3" in output or "ok" in output.lower()


def test_build_llm_activity_panel_sans_donnees_affiche_tirets() -> None:
    from trader.tui import _build_llm_activity_panel

    result = _build_llm_activity_panel({}, 0, None)
    assert isinstance(result, Panel)
    output = _render(result)
    # Sans données : calls_str = "—" et consolidation = "—"
    assert "—" in output
    # Le titre LLM doit être présent
    assert "LLM" in str(result.title)


def test_build_llm_activity_panel_avec_light_utilise_palette_light() -> None:
    from trader.tui import _build_llm_activity_panel

    result = _build_llm_activity_panel({}, 0, None, palette=PALETTE_LIGHT)
    # border_style doit correspondre au token border_llm_activity de PALETTE_LIGHT
    assert result.border_style == PALETTE_LIGHT["border_llm_activity"]
    _render(result)  # ne doit pas lever d'exception
    assert "LLM" in str(result.title)


# ---------------------------------------------------------------------------
# Task 6 — colonne data_source dans _build_decisions_table
# ---------------------------------------------------------------------------


def test_build_decisions_table_avec_data_source_affiche_colonne() -> None:
    from trader.tui import _build_decisions_table

    decisions = [
        {
            "symbol": "AAPL",
            "action": "BUY",
            "qty": 2.0,
            "rationale": "test",
            "confidence": 0.8,
            "data_source": "live",
        }
    ]
    console = Console(width=160)
    with console.capture() as cap:
        console.print(_build_decisions_table(decisions))
    output = cap.get()
    assert "live" in output


def test_build_decisions_table_sans_data_source_affiche_tiret() -> None:
    from trader.tui import _build_decisions_table

    decisions = [
        {
            "symbol": "TSLA",
            "action": "HOLD",
            "qty": 0.0,
            "rationale": "wait",
            "confidence": 0.5,
        }
    ]
    console = Console(width=160)
    with console.capture() as cap:
        console.print(_build_decisions_table(decisions))
    output = cap.get()
    # data_source absent → tiret dans la colonne
    assert "—" in output


def test_enrich_decisions_avec_recent_injecte_data_source() -> None:
    from trader.tui import _enrich_decisions_with_data_source

    decisions = [{"symbol": "SPY", "action": "BUY"}]
    recent = [
        {"symbol": "SPY", "runtime": {"data_source": "backfill"}},
        {"symbol": "QQQ", "runtime": {"data_source": "live"}},
    ]
    enriched = _enrich_decisions_with_data_source(decisions, recent)
    assert enriched[0].get("data_source") == "backfill"


def test_enrich_decisions_sans_match_laisse_data_source_none() -> None:
    from trader.tui import _enrich_decisions_with_data_source

    decisions = [{"symbol": "UNKNOWN", "action": "HOLD"}]
    recent = [{"symbol": "SPY", "runtime": {"data_source": "live"}}]
    enriched = _enrich_decisions_with_data_source(decisions, recent)
    assert enriched[0].get("data_source") is None


# ---------------------------------------------------------------------------
# Task 7 — load_runtime_state enrichi
# ---------------------------------------------------------------------------


def test_load_runtime_state_inclut_trade_plans(tmp_path) -> None:
    import json as _json

    from trader.tui import load_runtime_state

    (tmp_path / "trade_plans.json").write_text(
        _json.dumps(
            {
                "plans": [
                    {
                        "symbol": "AAPL",
                        "id": "x",
                        "side": "LONG",
                        "entry_price": 100.0,
                        "remaining_quantity": 1.0,
                        "quantity": 1.0,
                        "opened_at": "2026-06-10T10:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state = load_runtime_state(state_dir=tmp_path)
    assert "trade_plans" in state
    assert isinstance(state["trade_plans"], list)
    assert len(state["trade_plans"]) == 1


def test_load_runtime_state_inclut_indicator_watches(tmp_path) -> None:
    import json as _json

    from trader.tui import load_runtime_state

    sched = {
        "indicator_watches": {
            "A:1": {
                "id": "A:1",
                "symbol": "A",
                "expires_at": "2026-06-10T12:00:00Z",
                "logic": "any",
                "conditions": [],
            }
        },
        "stale_streaks": {"X": 2},
    }
    (tmp_path / "scheduler.json").write_text(_json.dumps(sched), encoding="utf-8")
    state = load_runtime_state(state_dir=tmp_path)
    assert "indicator_watches" in state
    assert isinstance(state["indicator_watches"], list)
    assert "stale_streaks" in state
    assert state["stale_streaks"].get("X") == 2


def test_load_runtime_state_inclut_recent_decisions(tmp_path) -> None:
    import json as _json

    from trader.tui import load_runtime_state

    rows = [
        {"symbol": "SPY", "action": "HOLD", "runtime": {"data_source": "live"}}
    ] * 5
    (tmp_path / "decisions.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows), encoding="utf-8"
    )
    state = load_runtime_state(state_dir=tmp_path)
    assert "recent_decisions" in state
    assert isinstance(state["recent_decisions"], list)
    assert len(state["recent_decisions"]) == 5


def test_load_runtime_state_inclut_recent_trips_depuis_attribution(tmp_path) -> None:
    import json as _json

    from trader.tui import load_runtime_state

    rows = [
        {"ts": "2026-06-15T10:00:00+00:00", "symbol": "AAPL", "action": "BUY",
         "quantity": 1, "price": 180.0, "confidence": 0.7, "intent": "OPEN_LONG"},
        {"ts": "2026-06-15T11:00:00+00:00", "symbol": "AAPL", "action": "SELL",
         "quantity": 1, "price": 185.0, "confidence": None, "intent": "PLANNED_EXIT",
         "exit_reason": "take_profit:tp1"},
    ]
    (tmp_path / "model_performance.jsonl").write_text(
        "\n".join(_json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    state = load_runtime_state(state_dir=tmp_path)

    assert "recent_trips" in state
    assert state["recent_trips"][0]["symbol"] == "AAPL"
    assert state["recent_trips"][0]["exit_reason"] == "take_profit:tp1"
    assert state["recent_trips"][0]["pnl"] == 5.0


def test_load_runtime_state_trade_plans_absent_retourne_vide(tmp_path) -> None:
    from trader.tui import load_runtime_state

    state = load_runtime_state(state_dir=tmp_path)
    assert state.get("trade_plans") == []
    assert state.get("indicator_watches") == []
    assert state.get("stale_streaks") == {}
    assert state.get("recent_decisions") == []
    assert state.get("learnings_pending_count") == 0
    assert state.get("consolidation_status") is None


def test_count_pending_learnings_ne_compte_que_apres_watermark(tmp_path) -> None:
    """Le buffer brut est rolling/plafonné — seul ce qui suit le watermark est pending."""
    import json

    from trader.tui import _count_pending_learnings_safe

    lp = tmp_path / "learnings.jsonl"
    cp = tmp_path / "learnings_consolidated.json"
    rows = [
        {"ts": "2026-06-15T10:00:00+00:00", "note": "consolidé1"},
        {"ts": "2026-06-15T11:00:00+00:00", "note": "consolidé2"},
        {"ts": "2026-06-15T13:00:00+00:00", "note": "pending1"},
        {"ts": "2026-06-15T14:00:00+00:00", "note": "pending2"},
    ]
    lp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    cp.write_text(
        json.dumps({"watermark": "2026-06-15T12:00:00+00:00", "global": [], "by_symbol": {}}),
        encoding="utf-8",
    )
    # 4 lignes dans le buffer, mais seules les 2 après 12:00 sont réellement pending
    assert _count_pending_learnings_safe(lp, cp) == 2


def test_count_pending_learnings_sans_consolidation_compte_tout(tmp_path) -> None:
    import json

    from trader.tui import _count_pending_learnings_safe

    lp = tmp_path / "learnings.jsonl"
    cp = tmp_path / "learnings_consolidated.json"  # absent → watermark None
    rows = [{"ts": "2026-06-15T10:00:00+00:00"}, {"ts": "2026-06-15T11:00:00+00:00"}]
    lp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert _count_pending_learnings_safe(lp, cp) == 2


def test_count_pending_learnings_fichier_absent(tmp_path) -> None:
    from trader.tui import _count_pending_learnings_safe

    assert _count_pending_learnings_safe(tmp_path / "nope.jsonl", tmp_path / "c.json") == 0


# ---------------------------------------------------------------------------
# D7 étage B — panneau plans armés
# ---------------------------------------------------------------------------


def _armed_watch(symbol: str = "CL=F") -> dict:
    return {
        "id": f"{symbol}:abc123",
        "symbol": symbol,
        "on_trigger": "EXECUTE_ORDER",
        "expires_at": "2026-06-11T16:00:00+00:00",
        "logic": "all",
        "conditions": [
            {"indicator": "z_score", "op": "abs>", "value": 2.0, "interval": "15m"}
        ],
        "order": {
            "intent": "OPEN_SHORT",
            "action": "SELL",
            "qty": 50.0,
            "confidence": 0.85,
            "exit_plan": {"hard_stop": {"type": "price", "price": 88.1}},
            "rationale": "cassure énergie",
        },
    }


def test_build_armed_plans_panel_affiche_sens_qty_stop_et_condition() -> None:
    from trader.tui import _build_armed_plans_panel

    rendered = _render(_build_armed_plans_panel([_armed_watch()]))

    assert "CL=F" in rendered
    assert "SHORT" in rendered
    assert "50" in rendered
    assert "88.1" in rendered  # le stop : la borne de risque, info clé opérateur
    assert "z_score" in rendered  # la condition de déclenchement


def test_build_armed_plans_panel_vide() -> None:
    from trader.tui import _build_armed_plans_panel

    rendered = _render(_build_armed_plans_panel([]))

    assert "aucun plan armé" in rendered


def test_build_armed_plans_panel_ignore_les_veilles_simples() -> None:
    from trader.tui import _build_armed_plans_panel

    simple = _armed_watch()
    simple["on_trigger"] = "WAKE"
    del simple["order"]

    rendered = _render(_build_armed_plans_panel([simple]))

    assert "aucun plan armé" in rendered


def test_build_watches_panel_exclut_les_plans_armes() -> None:
    # un plan armé n'est PAS une veille simple : il apparaît dans son propre
    # panneau, pas en double dans les veilles
    from trader.tui import _build_watches_panel

    rendered = _render(_build_watches_panel([_armed_watch()]))

    assert "aucune veille active" in rendered
