from __future__ import annotations

import json

from trader.application.universe.selection_attribution import (
    MIN_FEEDBACK_N,
    UniverseSelection,
    evaluate_selection,
    evaluate_selections,
    load_mandate_selections,
    refresh_selection_outcomes,
    resolve_bench,
    selection_feedback_digest,
    selections_from_mandate_payload,
    selections_from_mandate_payloads,
    summarize_outcomes,
)
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


class _FakeSource:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        return list(self.bars_by_symbol.get(symbol, []))


def _selection(**overrides) -> UniverseSelection:
    payload = {
        "mandate_id": "m-1",
        "symbol": "AIR.PA",
        "role": "core_candidate",
        "allowed_sides": ("long",),
        "as_of": "2026-01-01T08:00:00+00:00",
        "venue": "EU",
        "family": "defense_aero_eu",
    }
    payload.update(overrides)
    return UniverseSelection(**payload)


def _path_bars(start: float, end: float) -> list[Bar]:
    bars = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start) for day in range(1, 6)]
    bars.append(_bar("2026-01-06T16:00:00+00:00", end))
    return bars


def test_selection_long_haussiere_via_datasource_est_gagnante() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.002)
    source = _FakeSource({"AIR.PA": _path_bars(start, end)})
    evaluated = evaluate_selections([_selection()], source, horizon_sessions=5)
    assert len(evaluated) == 1
    assert evaluated[0].verdict == "gagnant"
    assert source.calls == [("AIR.PA", "1y", "1d")]


def test_selection_long_baissiere_via_datasource_est_perdante() -> None:
    start = 100.0
    end = start * (1.0 - SIGNIFICANT_RETURN_BAND - 0.002)
    item = evaluate_selection(_selection(), _path_bars(start, end), horizon_sessions=5)
    assert item is not None
    assert item.verdict == "perdant"


def test_selection_non_directionnelle_est_non_evaluable() -> None:
    empty = evaluate_selection(
        _selection(allowed_sides=()),
        _path_bars(100.0, 110.0),
        horizon_sessions=5,
    )
    both = evaluate_selection(
        _selection(allowed_sides=("long", "short")),
        _path_bars(100.0, 110.0),
        horizon_sessions=5,
    )
    assert empty is not None and empty.verdict == "non_evaluable"
    assert both is not None and both.verdict == "non_evaluable"


def test_selections_from_active_slice_and_full_mandate() -> None:
    slice_rows = selections_from_mandate_payload(
        {
            "mandate_ref": {
                "mandate_id": "m-eu",
                "venue": "EU",
                "as_of": "2026-07-11T08:00:00+00:00",
            },
            "symbol_mandate": {
                "symbol": "AIR.PA",
                "role": "core_candidate",
                "allowed_sides": ["long"],
                "family_context": {"family": "defense_aero_eu"},
            },
        }
    )
    full_rows = selections_from_mandate_payload(
        {
            "mandate_id": "m-eu",
            "venue": "EU",
            "as_of": "2026-07-11T08:00:00+00:00",
            "symbols": {
                "AIR.PA": {
                    "symbol": "AIR.PA",
                    "role": "core_candidate",
                    "allowed_sides": ["long"],
                    "family_context": {"family": "defense_aero_eu"},
                }
            },
        }
    )
    assert len(slice_rows) == 1
    assert slice_rows[0].family == "defense_aero_eu"
    assert slice_rows[0].allowed_sides == ("long",)
    assert full_rows == slice_rows


def test_summarize_outcomes_ignore_les_groupes_sous_le_plancher() -> None:
    summary = summarize_outcomes(
        [
            {
                "family": "alpha",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "forward_return": 0.02,
                "flair_score": 0.08,
                "horizon_sessions": 5,
            },
            {
                "family": "beta",
                "venue": "US",
                "role": "watch_only",
                "verdict": "perdant",
                "forward_return": -0.03,
                "flair_score": -0.08,
                "horizon_sessions": 5,
            },
        ]
    )
    assert summary["pays"]["families"] == []
    assert summary["decoit"]["families"] == []
    assert summary["n_gagnant"] == 1
    assert summary["n_perdant"] == 1
    assert {item["family"] for item in summary["by_family"]} == {"alpha", "beta"}


def test_summarize_outcomes_separe_pays_et_decoit_au_plancher() -> None:
    rows = [
        {
            "family": "alpha",
            "venue": "EU",
            "role": "core_candidate",
            "verdict": "gagnant",
            "forward_return": 0.02,
            "flair_score": 0.08,
            "horizon_sessions": 5,
        }
        for _ in range(MIN_FEEDBACK_N)
    ] + [
        {
            "family": "beta",
            "venue": "US",
            "role": "watch_only",
            "verdict": "perdant",
            "forward_return": -0.03,
            "flair_score": -0.08,
            "horizon_sessions": 5,
        }
        for _ in range(MIN_FEEDBACK_N)
    ]
    summary = summarize_outcomes(rows)
    assert summary["pays"]["families"] == ["alpha"]
    assert summary["decoit"]["families"] == ["beta"]


def test_selection_feedback_digest_reste_comparatif_et_filtre_la_venue() -> None:
    rows = [
        {
            "family": "eu_tech",
            "venue": "EU",
            "role": "core_candidate",
            "verdict": "gagnant",
            "forward_return": 0.02,
            "flair_score": 0.08,
            "horizon_sessions": 5,
        }
        for _ in range(MIN_FEEDBACK_N)
    ] + [
        {
            "family": "us_auto",
            "venue": "US",
            "role": "watch_only",
            "verdict": "perdant",
            "forward_return": -0.03,
            "flair_score": -0.08,
            "horizon_sessions": 5,
        }
        for _ in range(MIN_FEEDBACK_N)
    ]
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["role"] == "comparative_context_not_hotlist"
    assert digest["status"] == "observed"
    assert digest["families"] == [
        {
            "family": "eu_tech",
            "n": MIN_FEEDBACK_N,
            "win_rate": 1.0,
            "mean_flair_score": 0.08,
            "utility": "helps",
        }
    ]
    assert digest["roles"][0]["role"] == "core_candidate"
    assert "us_auto" not in {item["family"] for item in digest["families"]}


def test_selection_feedback_digest_insuffisant_sous_le_plancher() -> None:
    digest = selection_feedback_digest(
        [
            {
                "family": "eu_tech",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "flair_score": 0.08,
                "horizon_sessions": 5,
            }
        ],
        venue="EU",
    )
    assert digest["status"] == "insufficient"
    assert digest["families"] == []
    assert digest["n_evaluated"] == 1


def _mandate(
    *,
    status: str | None,
    mandate_id: str = "m-eu",
    scope_id: str = "scope-eu",
    as_of: str = "2026-08-10T06:45:00+00:00",
    symbols: tuple[str, ...] = ("AIR.PA", "MC.PA"),
    fallback_reason: str | None = None,
) -> dict:
    payload: dict = {
        "mandate_id": mandate_id,
        "candidate_scope_id": scope_id,
        "venue": "EU",
        "as_of": as_of,
        "symbols": {
            symbol: {
                "symbol": symbol,
                "role": "core_candidate",
                "allowed_sides": ["long"],
                "family_context": {"family": "defense_aero_eu"},
            }
            for symbol in symbols
        },
        "fallback_reason": fallback_reason,
    }
    if status is not None:
        payload["status"] = status
    return payload


def test_selections_extrait_scope_status_et_selector() -> None:
    rows = selections_from_mandate_payload(_mandate(status="active"))
    assert {row.symbol for row in rows} == {"AIR.PA", "MC.PA"}
    assert all(row.candidate_scope_id == "scope-eu" for row in rows)
    assert all(row.status == "active" for row in rows)
    assert all(row.selector == "agent" for row in rows)
    assert all(row.selected_symbols == ("AIR.PA", "MC.PA") for row in rows)


def test_prepared_est_exclu_de_l_unite_de_jugement() -> None:
    assert selections_from_mandate_payload(_mandate(status="prepared")) == []


def test_fallback_est_tague_baseline_et_raison() -> None:
    rows = selections_from_mandate_payload(
        _mandate(status="fallback", fallback_reason="agent_brief_missing")
    )
    assert len(rows) == 2
    assert all(row.selector == "baseline_fallback" for row in rows)
    assert all(row.status == "fallback" for row in rows)


def test_payload_sans_status_est_juge_comme_activation_legacy() -> None:
    # Historique live 2026-08-16 : 0/476 lignes sans status. Les fixtures et
    # slices actives sans champ restent des activations, jamais des prepared.
    rows = selections_from_mandate_payload(_mandate(status=None))
    assert [row.symbol for row in rows] == ["AIR.PA", "MC.PA"]
    assert all(row.selector == "agent" for row in rows)


def test_slice_active_lit_scope_et_status_depuis_mandate_ref() -> None:
    rows = selections_from_mandate_payload(
        {
            "mandate_ref": {
                "mandate_id": "m-eu",
                "candidate_scope_id": "scope-eu",
                "venue": "EU",
                "as_of": "2026-08-10T06:45:00+00:00",
                "status": "active",
            },
            "symbol_mandate": {
                "symbol": "AIR.PA",
                "role": "core_candidate",
                "allowed_sides": ["long"],
                "family_context": {"family": "defense_aero_eu"},
            },
        }
    )
    assert len(rows) == 1
    assert rows[0].candidate_scope_id == "scope-eu"
    assert rows[0].status == "active"
    assert rows[0].selector == "agent"


def test_rafale_fallback_dedup_par_scope_et_symbole() -> None:
    burst = [
        _mandate(
            status="fallback",
            mandate_id=f"universe-mandate:fallback:{index}",
            as_of=f"2026-07-17T06:{45 + index:02d}:00+00:00",
            fallback_reason="agent_brief_missing",
        )
        for index in range(3)
    ]
    rows = selections_from_mandate_payloads(burst)
    assert sorted(row.symbol for row in rows) == ["AIR.PA", "MC.PA"]
    assert all(row.selector == "baseline_fallback" for row in rows)


def test_deux_activations_agent_du_meme_scope_restent_distinctes() -> None:
    rows = selections_from_mandate_payloads(
        [
            _mandate(status="active", mandate_id="m-1", as_of="2026-08-10T06:45:00+00:00"),
            _mandate(status="active", mandate_id="m-1", as_of="2026-08-11T06:45:00+00:00"),
        ]
    )
    assert len(rows) == 4


def test_load_mandate_selections_ignore_prepared(tmp_path) -> None:
    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(_mandate(status="prepared", mandate_id="m-prep"))
        + "\n"
        + json.dumps(_mandate(status="active", mandate_id="m-act", symbols=("AIR.PA",)))
        + "\n",
        encoding="utf-8",
    )
    rows = load_mandate_selections(tmp_path)
    assert [row.symbol for row in rows] == ["AIR.PA"]
    assert rows[0].status == "active"


def test_resolve_bench_retire_picks_et_sticky() -> None:
    selection = _selection(
        symbol="AIR.PA",
        candidate_scope_id="scope-eu",
        selected_symbols=("AIR.PA", "MC.PA"),
    )
    bench = resolve_bench(
        selection,
        {
            "candidates": [
                {"symbol": "AIR.PA"},
                {"symbol": "MC.PA"},
                {"symbol": "OR.PA"},
                {"symbol": "SAN.PA"},
                {"symbol": "DBK.DE"},
            ],
            "sticky_context_at_close": ["DBK.DE", "MSFT"],
        },
    )
    assert bench == ["OR.PA", "SAN.PA"]


def test_resolve_bench_sans_sticky_ni_pick_garde_l_ordre_des_candidats() -> None:
    bench = resolve_bench(
        _selection(symbol="AIR.PA", selected_symbols=("AIR.PA",)),
        {"candidates": [{"symbol": "OR.PA"}, {"symbol": "AIR.PA"}, {"symbol": "SAN.PA"}]},
    )
    assert bench == ["OR.PA", "SAN.PA"]


def test_refresh_ouvre_le_store_par_injection(tmp_path) -> None:
    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            _mandate(
                status="active",
                symbols=("AIR.PA",),
                as_of="2026-01-01T08:00:00+00:00",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    class _Store:
        def __init__(self) -> None:
            self.rows: list[dict] = []
            self.scores: dict[int, float] = {}

        def upsert_outcomes(self, rows) -> None:
            self.rows.extend(dict(row) for row in rows)

        def load_outcomes(self) -> list[dict]:
            return [{"id": index, **row} for index, row in enumerate(self.rows, start=1)]

        def update_flair_scores(self, scores) -> None:
            self.scores = dict(scores)

        def count(self) -> int:
            return len(self.rows)

    store = _Store()
    source = _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)})
    result = refresh_selection_outcomes(tmp_path, source, store_opener=lambda _state_dir: store)
    assert result["pending"] == 1
    assert result["evaluated"] == 1
    assert result["stored"] == 1
    assert store.rows[0]["symbol"] == "AIR.PA"
    assert store.rows[0]["verdict"] == "gagnant"
