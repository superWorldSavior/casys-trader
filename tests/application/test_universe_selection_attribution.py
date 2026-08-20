from __future__ import annotations

import json

import pytest

from trader.application.universe.selection_attribution import (
    MIN_FEEDBACK_N,
    EvaluatedSelection,
    UniverseSelection,
    evaluate_selection,
    evaluate_selections,
    load_mandate_selections,
    persist_and_score,
    refresh_selection_outcomes,
    resolve_bench,
    selection_feedback_digest,
    selections_from_mandate_payload,
    selections_from_mandate_payloads,
    summarize_outcomes,
)
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar, MarketError
from trader.domain.universe.selection_attribution import (
    MIN_BENCH_EVALUATED,
    SELECTION_SEMANTICS_VERSION,
)


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


class _FakeSource:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        return list(self.bars_by_symbol.get(symbol, []))


class _ErrorSource:
    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        raise MarketError("fetch_failed", f"{symbol}: 429")


class _ErrorSource:
    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        raise MarketError("fetch_failed", f"{symbol}: 429")


class _SelectiveErrorSource(_FakeSource):
    def __init__(
        self,
        bars_by_symbol: dict[str, list[Bar]],
        *,
        failed_symbols: set[str],
    ) -> None:
        super().__init__(bars_by_symbol)
        self.failed_symbols = failed_symbols

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        if symbol in self.failed_symbols:
            raise MarketError("fetch_failed", f"{symbol}: 429")
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


def _direction(rows: list) -> object:
    found = [row for row in rows if row.verdict_basis == "direction"]
    assert len(found) == 1
    return found[0]


def _allocation(rows: list) -> object:
    found = [row for row in rows if row.verdict_basis == "allocation"]
    assert len(found) == 1
    return found[0]


def test_selection_long_haussiere_via_datasource_est_gagnante() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.002)
    source = _FakeSource({"AIR.PA": _path_bars(start, end)})
    evaluated = evaluate_selections([_selection()], source, horizon_sessions=5)
    assert _direction(evaluated).verdict == "gagnant"
    assert source.calls == [("AIR.PA", "1y", "1d")]


def test_selection_long_baissiere_via_datasource_est_perdante() -> None:
    start = 100.0
    end = start * (1.0 - SIGNIFICANT_RETURN_BAND - 0.002)
    rows = evaluate_selection(_selection(), _path_bars(start, end), horizon_sessions=5)
    assert _direction(rows).verdict == "perdant"


def test_selection_non_directionnelle_n_a_pas_de_ligne_direction() -> None:
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
    assert [row.verdict_basis for row in empty] == ["allocation"]
    assert empty[0].verdict == "non_evaluable"
    assert [row.verdict_basis for row in both] == ["allocation"]
    assert both[0].verdict == "non_evaluable"


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
            "verdict_basis": "direction",
            "selector": "agent",
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
            "verdict_basis": "direction",
            "selector": "agent",
        }
        for _ in range(MIN_FEEDBACK_N)
    ]
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["role"] == "comparative_context_not_hotlist"
    assert digest["status"] == "observed"
    assert digest["families"] == [
        {
            "family": "eu_tech",
            "direction": {
                "n": MIN_FEEDBACK_N,
                "win_rate": 1.0,
                "mean_flair_score": 0.08,
                "utility": "helps",
            },
        }
    ]
    assert digest["roles"][0]["role"] == "core_candidate"
    assert "allocation" not in digest["families"][0]
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
                "verdict_basis": "direction",
                "selector": "agent",
            }
        ],
        venue="EU",
    )
    assert digest["status"] == "insufficient"
    assert digest["families"] == []
    assert digest["n_evaluated"] == 1


def test_selection_feedback_digest_deux_blocs_par_base() -> None:
    rows = [
        *(
            {
                "family": "energie",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "flair_score": 0.04,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "agent",
            }
            for _ in range(MIN_FEEDBACK_N)
        ),
        *(
            {
                "family": "energie",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "perdant",
                "flair_score": -0.02,
                "horizon_sessions": 5,
                "verdict_basis": "direction",
                "selector": "agent",
            }
            for _ in range(MIN_FEEDBACK_N)
        ),
    ]
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["status"] == "observed"
    assert digest["families"] == [
        {
            "family": "energie",
            "allocation": {
                "n": MIN_FEEDBACK_N,
                "beat_bench_rate": 1.0,
                "mean_flair_score": 0.04,
                "utility": "helps",
            },
            "direction": {
                "n": MIN_FEEDBACK_N,
                "win_rate": 0.0,
                "mean_flair_score": -0.02,
                "utility": "hurts",
            },
        }
    ]


def test_selection_feedback_digest_ignore_le_baseline_et_min_n_par_base() -> None:
    rows = [
        *(
            {
                "family": "energie",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "flair_score": 0.04,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "agent",
            }
            for _ in range(MIN_FEEDBACK_N)
        ),
        *(
            {
                "family": "energie",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "flair_score": 0.04,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "baseline_fallback",
            }
            for _ in range(MIN_FEEDBACK_N)
        ),
        *(
            {
                "family": "energie",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "perdant",
                "flair_score": -0.02,
                "horizon_sessions": 5,
                "verdict_basis": "direction",
                "selector": "agent",
            }
            for _ in range(MIN_FEEDBACK_N - 1)
        ),
    ]
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["status"] == "observed"
    assert digest["families"][0]["allocation"]["n"] == MIN_FEEDBACK_N
    assert "direction" not in digest["families"][0]
    assert digest["n_evaluated"] == MIN_FEEDBACK_N + (MIN_FEEDBACK_N - 1)
    assert digest["allocation"]["n"] == MIN_FEEDBACK_N
    assert digest["allocation"]["vs_baseline"]["n"] == MIN_FEEDBACK_N
    assert digest["allocation"]["vs_baseline"]["lift"] == 0.0


def test_digest_allocation_venue_parle_sans_min_n_famille() -> None:
    rows = []
    for family, n in (("eu_tech", 2), ("eu_industrials", 2), ("defense", 1)):
        rows.extend(
            {
                "family": family,
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "flair_score": 0.03,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "agent",
            }
            for _ in range(n)
        )
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["status"] == "observed"
    assert digest["families"] == []
    assert digest["allocation"]["n"] == MIN_FEEDBACK_N
    assert digest["allocation"]["beat_bench_rate"] == 1.0
    assert digest["allocation"]["utility"] == "helps"
    assert "vs_baseline" not in digest["allocation"]


def test_digest_vs_baseline_absent_si_controle_sous_le_plancher() -> None:
    rows = [
        *(
            {
                "family": "eu_tech",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "flair_score": 0.04,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "agent",
            }
            for _ in range(MIN_FEEDBACK_N)
        ),
        {
            "family": "eu_tech",
            "venue": "EU",
            "role": "fallback_selection",
            "verdict": "perdant",
            "flair_score": -0.02,
            "horizon_sessions": 5,
            "verdict_basis": "allocation",
            "selector": "baseline_fallback",
        },
    ]
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["allocation"]["n"] == MIN_FEEDBACK_N
    assert "vs_baseline" not in digest["allocation"]


def test_digest_vs_baseline_est_un_lift_de_taux_pas_un_flair_mixe() -> None:
    rows = [
        *(
            {
                "family": "eu_tech",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant" if index < 4 else "perdant",
                "flair_score": 0.02 if index < 4 else -0.02,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "agent",
            }
            for index in range(MIN_FEEDBACK_N)
        ),
        *(
            {
                "family": "eu_tech",
                "venue": "EU",
                "role": "fallback_selection",
                "verdict": "perdant",
                "flair_score": -0.01,
                "horizon_sessions": 5,
                "verdict_basis": "allocation",
                "selector": "baseline_fallback",
            }
            for _ in range(MIN_FEEDBACK_N)
        ),
    ]
    digest = selection_feedback_digest(rows, venue="EU")
    assert digest["allocation"]["beat_bench_rate"] == 0.8
    assert digest["allocation"]["vs_baseline"]["beat_bench_rate"] == 0.0
    assert digest["allocation"]["vs_baseline"]["lift"] == 0.8
    assert "mean_flair_score" not in digest["allocation"]["vs_baseline"]
    dumped = str(digest)
    assert "baseline_fallback" not in dumped
    assert "direction_source" not in dumped


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

        def ensure_selection_semantics(self) -> str:
            return SELECTION_SEMANTICS_VERSION

    class _Scopes:
        def read_by_id(self, candidate_scope_id: str, hint_date: str | None = None):
            return None

    store = _Store()
    source = _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)})
    result = refresh_selection_outcomes(
        tmp_path,
        source,
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _Scopes(),
    )
    assert result["pending"] == 1
    assert result["evaluated"] == 2
    assert result["stored"] == 2
    assert result["bench_unresolved"] == 1
    assert result["semantics_version"] == SELECTION_SEMANTICS_VERSION
    bases = {row["verdict_basis"]: row for row in store.rows}
    assert bases["direction"]["verdict"] == "gagnant"
    assert bases["allocation"]["verdict"] == "non_evaluable"
    assert bases["direction"]["selector"] == "agent"


class _ScopeReader:
    def __init__(self, scopes: dict[str, dict | None]) -> None:
        self.scopes = scopes
        self.calls: list[tuple[str, str | None]] = []

    def read_by_id(self, candidate_scope_id: str, hint_date: str | None = None):
        self.calls.append((candidate_scope_id, hint_date))
        return self.scopes.get(candidate_scope_id)


def _fat_scope(*, pick: str = "AIR.PA", n: int = 10, sticky: tuple[str, ...] = ()) -> dict:
    candidates = [{"symbol": pick}, *({"symbol": f"B{index}.PA"} for index in range(n))]
    return {
        "candidate_scope_id": "scope-eu",
        "candidates": list(candidates),
        "sticky_context_at_close": list(sticky),
    }


def test_horizon_immature_ne_persiste_aucune_base() -> None:
    rows = evaluate_selection(
        _selection(),
        [_bar("2026-01-01T16:00:00+00:00", 100.0)],
        horizon_sessions=5,
        bench_opportunities=[0.01] * MIN_BENCH_EVALUATED,
    )
    assert rows == []


def test_pick_sans_barres_allocation_non_evaluable_definitive() -> None:
    rows = evaluate_selection(_selection(), [], horizon_sessions=5, bench_opportunities=[0.01] * MIN_BENCH_EVALUATED)
    assert [row.verdict_basis for row in rows] == ["allocation"]
    assert rows[0].verdict == "non_evaluable"


def test_banc_structurellement_sous_le_plancher_reste_non_evaluable() -> None:
    rows = evaluate_selection(
        _selection(),
        _path_bars(100.0, 110.0),
        horizon_sessions=5,
        bench_opportunities=[0.01] * (MIN_BENCH_EVALUATED - 1),
    )
    allocation = _allocation(rows)
    assert allocation.verdict == "non_evaluable"
    assert allocation.bench_n == MIN_BENCH_EVALUATED - 1


def test_market_error_ne_persiste_pas_de_non_evaluable() -> None:
    rows = evaluate_selections(
        [_selection(candidate_scope_id="scope-eu")],
        _ErrorSource(),
        horizon_sessions=5,
        scope_reader=_ScopeReader({"scope-eu": _fat_scope()}),
    )
    assert rows == []


def test_refresh_reessaie_apres_market_error(tmp_path) -> None:
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

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
    store = try_open_universe_selection_store(tmp_path)
    first = refresh_selection_outcomes(
        tmp_path,
        _ErrorSource(),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({}),
    )
    assert first["evaluated"] == 0
    assert store.count() == 0
    second = refresh_selection_outcomes(
        tmp_path,
        _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)}),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({}),
    )
    assert second["evaluated"] >= 1
    assert store.count() >= 1
    assert any(row["verdict_basis"] == "direction" for row in store.load_outcomes())


def test_refresh_sans_nouvelle_cle_repare_un_flair_non_persiste(tmp_path) -> None:
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

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
    store = try_open_universe_selection_store(tmp_path)
    update_flair_scores = store.update_flair_scores
    first_update = True

    def fail_once(scores) -> None:
        nonlocal first_update
        if first_update:
            first_update = False
            raise RuntimeError("interrupted_after_outcome_upsert")
        update_flair_scores(scores)

    store.update_flair_scores = fail_once
    source = _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)})

    with pytest.raises(RuntimeError, match="interrupted_after_outcome_upsert"):
        refresh_selection_outcomes(
            tmp_path,
            source,
            store_opener=lambda _state_dir: store,
            scope_store_opener=lambda _state_dir: _ScopeReader({}),
        )

    first_rows = store.load_outcomes()
    assert {row["verdict_basis"] for row in first_rows} == {"allocation", "direction"}
    assert all(row["flair_score"] is None for row in first_rows)

    repaired = refresh_selection_outcomes(
        tmp_path,
        source,
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({}),
    )

    assert repaired["pending"] == 0
    assert repaired["evaluated"] == 0
    assert repaired["progressed"] == 0
    by_basis = {row["verdict_basis"]: row for row in store.load_outcomes()}
    assert by_basis["direction"]["flair_score"] is not None


def test_refresh_reessaie_allocation_apres_market_error_sur_un_membre_du_banc(
    tmp_path,
) -> None:
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            _mandate(
                status="active",
                symbols=("AIR.PA",),
                as_of="2026-01-01T08:00:00+00:00",
                scope_id="scope-eu",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    scope = _fat_scope(n=MIN_BENCH_EVALUATED)
    bench_symbols = {f"B{index}.PA" for index in range(MIN_BENCH_EVALUATED)}
    bars_by_symbol = {
        "AIR.PA": _path_bars(100.0, 110.0),
        **{symbol: _path_bars(100.0, 101.0) for symbol in bench_symbols},
    }
    store = try_open_universe_selection_store(tmp_path)

    first = refresh_selection_outcomes(
        tmp_path,
        _SelectiveErrorSource(
            bars_by_symbol,
            failed_symbols={f"B{MIN_BENCH_EVALUATED - 1}.PA"},
        ),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({"scope-eu": scope}),
    )

    assert first["evaluated"] == 1
    assert first["progressed"] == 1
    direction_before = next(
        row for row in store.load_outcomes() if row["verdict_basis"] == "direction"
    )

    second = refresh_selection_outcomes(
        tmp_path,
        _FakeSource(bars_by_symbol),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({"scope-eu": scope}),
    )

    assert second["progressed"] == 1
    by_basis = {row["verdict_basis"]: row for row in store.load_outcomes()}
    assert set(by_basis) == {"allocation", "direction"}
    assert by_basis["direction"]["evaluated_at"] == direction_before["evaluated_at"]
    assert by_basis["allocation"]["verdict"] == "gagnant"
    assert by_basis["allocation"]["bench_n"] == MIN_BENCH_EVALUATED


def test_refresh_garde_un_banc_immature_pending_puis_complete_allocation(
    tmp_path,
) -> None:
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            _mandate(
                status="active",
                symbols=("AIR.PA",),
                as_of="2026-01-01T08:00:00+00:00",
                scope_id="scope-eu",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    scope = _fat_scope(n=MIN_BENCH_EVALUATED)
    bench_symbols = {f"B{index}.PA" for index in range(MIN_BENCH_EVALUATED)}
    mature_bars = {
        "AIR.PA": _path_bars(100.0, 110.0),
        **{symbol: _path_bars(100.0, 101.0) for symbol in bench_symbols},
    }
    immature_bars = {
        **mature_bars,
        **{
            symbol: [_bar("2026-01-01T16:00:00+00:00", 100.0)]
            for symbol in bench_symbols
        },
    }
    store = try_open_universe_selection_store(tmp_path)

    first = refresh_selection_outcomes(
        tmp_path,
        _FakeSource(immature_bars),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({"scope-eu": scope}),
    )

    assert first["evaluated"] == 1
    assert first["progressed"] == 1
    revision = "2026-08-20T00:00:00+00:00"
    with store._db.transaction() as cur:
        cur.execute(
            "UPDATE universe_selection_outcomes SET evaluated_at=? WHERE verdict_basis='direction'",
            (revision,),
        )

    still_immature = refresh_selection_outcomes(
        tmp_path,
        _FakeSource(
            {
                **immature_bars,
                "AIR.PA": _path_bars(100.0, 90.0),
            }
        ),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({"scope-eu": scope}),
    )

    assert still_immature["evaluated"] == 1
    assert still_immature["progressed"] == 0
    assert store.count() == 1
    assert store.load_outcomes()[0]["evaluated_at"] == revision
    assert store.load_outcomes()[0]["verdict"] == "gagnant"

    mature = refresh_selection_outcomes(
        tmp_path,
        _FakeSource(mature_bars),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({"scope-eu": scope}),
    )

    assert mature["evaluated"] == 2
    assert mature["progressed"] == 1
    by_basis = {row["verdict_basis"]: row for row in store.load_outcomes()}
    assert set(by_basis) == {"allocation", "direction"}
    assert by_basis["direction"]["evaluated_at"] == revision
    assert by_basis["allocation"]["bench_n"] == MIN_BENCH_EVALUATED


def test_refresh_cursor_durable_ne_starve_pas_le_129e_retryable(
    tmp_path,
    monkeypatch,
) -> None:
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

    selections = [
        _selection(
            mandate_id=f"m-{index:03d}",
            symbol=f"S{index:03d}",
            candidate_scope_id="scope-missing",
        )
        for index in range(129)
    ]
    monkeypatch.setattr(
        "trader.application.universe.selection_attribution.load_mandate_selections",
        lambda _state_dir: selections,
    )
    immature = [_bar("2026-01-01T16:00:00+00:00", 100.0)]
    source = _FakeSource(
        {
            **{selection.symbol: immature for selection in selections[:-1]},
            selections[-1].symbol: _path_bars(100.0, 110.0),
        }
    )
    store = try_open_universe_selection_store(tmp_path)

    first = refresh_selection_outcomes(
        tmp_path,
        source,
        limit=128,
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({}),
    )

    assert first["pending"] == 128
    assert first["progressed"] == 0
    assert first["cursor"] == 128
    assert store.selection_refresh_cursor() == 128

    second = refresh_selection_outcomes(
        tmp_path,
        source,
        limit=128,
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({}),
    )

    assert second["pending"] == 128
    assert second["progressed"] == 2
    assert second["cursor"] == 127
    assert store.selection_refresh_cursor() == 127
    rows = store.load_outcomes()
    assert {row["symbol"] for row in rows} == {"S128"}
    assert {row["verdict_basis"] for row in rows} == {"allocation", "direction"}


def test_refresh_complete_la_direction_quand_allocation_existe(tmp_path) -> None:
    from datetime import datetime, timezone

    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            _mandate(
                status="active",
                mandate_id="m-eu",
                symbols=("AIR.PA",),
                as_of="2026-01-01T08:00:00+00:00",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    store = try_open_universe_selection_store(tmp_path)
    persist_and_score(
        store,
        [
            EvaluatedSelection(
                mandate_id="m-eu",
                symbol="AIR.PA",
                role="core_candidate",
                allowed_sides=("long",),
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                family="defense_aero_eu",
                horizon_sessions=5,
                forward_return=0.02,
                verdict="non_evaluable",
                verdict_basis="allocation",
                selector="agent",
            )
        ],
        now=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    assert {row["verdict_basis"] for row in store.load_outcomes()} == {"allocation"}
    refresh_selection_outcomes(
        tmp_path,
        _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)}),
        store_opener=lambda _state_dir: store,
        scope_store_opener=lambda _state_dir: _ScopeReader({}),
    )
    bases = {row["verdict_basis"] for row in store.load_outcomes()}
    assert bases == {"allocation", "direction"}


def test_allocation_gagne_contre_le_banc() -> None:
    bench = [0.01] * MIN_BENCH_EVALUATED
    rows = evaluate_selection(
        _selection(candidate_scope_id="scope-eu", selector="agent"),
        _path_bars(100.0, 110.0),
        horizon_sessions=5,
        bench_opportunities=bench,
    )
    allocation = _allocation(rows)
    assert _direction(rows).verdict == "gagnant"
    assert allocation.verdict == "gagnant"
    assert allocation.selector == "agent"
    assert allocation.opportunity == pytest.approx(0.1)
    assert allocation.bench_n == MIN_BENCH_EVALUATED
    assert allocation.bench_median_opportunity == 0.01


def test_scope_introuvable_compte_bench_unresolved() -> None:
    selection = _selection(candidate_scope_id="scope-missing")
    source = _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)})
    reader = _ScopeReader({})
    rows = evaluate_selections([selection], source, horizon_sessions=5, scope_reader=reader)
    assert reader.calls == [("scope-missing", "2026-01-01")]
    assert _direction(rows).verdict == "gagnant"
    assert _allocation(rows).verdict == "non_evaluable"
    assert _allocation(rows).scope_missing is True


def test_cache_opportunites_par_scope_ne_refetch_pas_le_banc() -> None:
    scope = _fat_scope(n=MIN_BENCH_EVALUATED)
    bench_symbols = [f"B{index}.PA" for index in range(MIN_BENCH_EVALUATED)]
    bars = {"AIR.PA": _path_bars(100.0, 110.0), "MC.PA": _path_bars(100.0, 110.0)}
    bars.update({symbol: _path_bars(100.0, 101.0) for symbol in bench_symbols})
    source = _FakeSource(bars)
    first = _selection(symbol="AIR.PA", candidate_scope_id="scope-eu", selected_symbols=("AIR.PA", "MC.PA"))
    second = _selection(
        mandate_id="m-2",
        symbol="MC.PA",
        candidate_scope_id="scope-eu",
        selected_symbols=("AIR.PA", "MC.PA"),
    )
    rows = evaluate_selections(
        [first, second],
        source,
        horizon_sessions=5,
        scope_reader=_ScopeReader({"scope-eu": scope}),
    )
    fetched = [symbol for symbol, _lookback, _interval in source.calls]
    assert fetched.count("AIR.PA") == 1
    assert fetched.count("MC.PA") == 1
    assert all(fetched.count(symbol) == 1 for symbol in bench_symbols)
    allocations = [row for row in rows if row.verdict_basis == "allocation"]
    assert len(allocations) == 2
    assert all(row.verdict == "gagnant" for row in allocations)


def test_refresh_pending_evaluated_bench_unresolved(tmp_path) -> None:
    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            _mandate(
                status="active",
                symbols=("AIR.PA",),
                as_of="2026-01-01T08:00:00+00:00",
                scope_id="scope-eu",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    class _Store:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def upsert_outcomes(self, rows) -> None:
            self.rows.extend(dict(row) for row in rows)

        def load_outcomes(self) -> list[dict]:
            return [{"id": index, **row} for index, row in enumerate(self.rows, start=1)]

        def update_flair_scores(self, scores) -> None:
            return None

        def count(self) -> int:
            return len(self.rows)

        def ensure_selection_semantics(self) -> str:
            return SELECTION_SEMANTICS_VERSION

    scope = _fat_scope(n=MIN_BENCH_EVALUATED)
    bench_symbols = [f"B{index}.PA" for index in range(MIN_BENCH_EVALUATED)]
    source = _FakeSource(
        {
            "AIR.PA": _path_bars(100.0, 110.0),
            **{symbol: _path_bars(100.0, 101.0) for symbol in bench_symbols},
        }
    )
    result = refresh_selection_outcomes(
        tmp_path,
        source,
        store_opener=lambda _state_dir: _Store(),
        scope_store_opener=lambda _state_dir: _ScopeReader({"scope-eu": scope}),
    )
    assert result["pending"] == 1
    assert result["evaluated"] == 2
    assert result["stored"] == 2
    assert result["bench_unresolved"] == 0
    assert result["semantics_version"] == SELECTION_SEMANTICS_VERSION
