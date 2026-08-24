from __future__ import annotations

from trader.application.analyst.situation_attribution import (
    MIN_FEEDBACK_N,
    SITUATION_FEEDBACK_ROLE,
    evaluate_note,
    evaluate_notes,
    refresh_situation_outcomes,
    situation_feedback_digest,
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


def _path_bars(start: float, end: float) -> list[Bar]:
    bars = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start) for day in range(1, 6)]
    bars.append(_bar("2026-01-06T16:00:00+00:00", end))
    return bars


def _note(**overrides) -> dict:
    payload = {
        "id": 4,
        "as_of": "2026-01-01T08:00:00+00:00",
        "venue": "EU",
        "section_type": "family",
        "section_name": "eu_industrials",
        "direction": "bullish",
        "horizon": "court terme",
        "symbols": '["AIR.PA"]',
    }
    payload.update(overrides)
    return payload


def test_famille_bullish_haussiere_via_datasource_est_gagnante() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.002)
    families = {"eu_industrials": ["AIR.PA", "SIE.DE"]}
    source = _FakeSource({"AIR.PA": _path_bars(start, end), "SIE.DE": _path_bars(start, end)})
    evaluated = evaluate_notes([_note()], source, families=families)
    assert len(evaluated) == 1
    assert evaluated[0].verdict == "gagnant"
    assert evaluated[0].coverage_n == 2
    assert evaluated[0].horizon_sessions == 5
    assert {call[0] for call in source.calls} == {"AIR.PA", "SIE.DE"}
    assert source.calls[0][1:] == ("1y", "1d")


def test_horizon_immatures_sont_sautes() -> None:
    item = evaluate_note(
        _note(horizon="1-10d"),
        {"AIR.PA": _path_bars(100.0, 110.0)},
        families={"eu_industrials": ["AIR.PA"]},
    )
    assert item is None


def test_mixed_et_zone_sont_non_evaluables_sans_fetch() -> None:
    source = _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)})
    mixed = evaluate_notes(
        [_note(direction="mixed", horizon="court terme")],
        source,
        families={"eu_industrials": ["AIR.PA"]},
    )
    zone = evaluate_notes(
        [_note(section_type="zone", section_name="EU", direction="bullish", horizon="court terme")],
        source,
        families={"eu_industrials": ["AIR.PA"]},
    )
    assert mixed[0].verdict == "non_evaluable"
    assert zone[0].verdict == "non_evaluable"
    assert source.calls == []


def test_alerte_sans_symbole_non_evaluable() -> None:
    item = evaluate_note(
        _note(section_type="alert", section_name="alerts", symbols="[]", horizon="short"),
        {},
        families={},
    )
    assert item is not None
    assert item.verdict == "non_evaluable"


def test_summarize_outcomes_separe_pays_et_decoit() -> None:
    summary = summarize_outcomes(
        [
            {
                "family": "eu_industrials",
                "direction": "bullish",
                "section_type": "family",
                "section_name": "eu_industrials",
                "verdict": "gagnant",
                "forward_return": 0.02,
                "outcome_score": 0.08,
                "horizon_sessions": 10,
            },
            {
                "family": "tw_osat",
                "direction": "bearish",
                "section_type": "family",
                "section_name": "tw_osat",
                "verdict": "perdant",
                "forward_return": 0.03,
                "outcome_score": -0.08,
                "horizon_sessions": 5,
            },
        ]
    )
    assert summary["pays"]["families"] == ["eu_industrials"]
    assert summary["decoit"]["families"] == ["tw_osat"]
    assert summary["n_gagnant"] == 1
    assert summary["n_perdant"] == 1
    buckets = {item["horizon_bucket"] for item in summary["by_horizon"]}
    assert buckets == {"semaines", "court"}


def _scored_row(**overrides) -> dict:
    payload = {
        "family": "eu_industrials",
        "venue": "EU",
        "direction": "bullish",
        "section_type": "family",
        "section_name": "eu_industrials",
        "verdict": "gagnant",
        "forward_return": 0.02,
        "outcome_score": 0.08,
        "horizon_sessions": 5,
    }
    payload.update(overrides)
    return payload


def test_situation_feedback_digest_reste_comparatif_et_filtre_la_venue() -> None:
    rows = [
        _scored_row()
        for _ in range(MIN_FEEDBACK_N)
    ] + [
        _scored_row(
            family="us_auto",
            venue="US",
            direction="bearish",
            section_type="symbol",
            section_name="TSLA",
            verdict="perdant",
            forward_return=-0.03,
            outcome_score=-0.08,
        )
        for _ in range(MIN_FEEDBACK_N)
    ]
    digest = situation_feedback_digest(rows, venue="EU")
    assert digest["role"] == SITUATION_FEEDBACK_ROLE
    assert digest["status"] == "observed"
    assert digest["directions"] == [
        {
            "direction": "bullish",
            "n": MIN_FEEDBACK_N,
            "win_rate": 1.0,
            "mean_outcome_score": 0.08,
            "utility": "helps",
        }
    ]
    assert digest["section_types"][0]["section_type"] == "family"
    assert digest["families"][0]["family"] == "eu_industrials"
    assert "us_auto" not in {item["family"] for item in digest["families"]}
    serialized = str(digest)
    assert "TSLA" not in serialized
    assert "AIR.PA" not in serialized


def test_situation_feedback_digest_insuffisant_sous_le_plancher() -> None:
    digest = situation_feedback_digest(
        [_scored_row()],
        venue="EU",
    )
    assert digest["status"] == "insufficient"
    assert digest["directions"] == []
    assert digest["section_types"] == []
    assert digest["families"] == []
    assert digest["n_evaluated"] == 1


def test_refresh_ne_fige_pas_une_note_immature(tmp_path) -> None:
    from trader.domain.situation import NewsMacroBrief
    from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore

    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-01-01T08:00:00+00:00|EU",
            "venue": "EU",
            "as_of": "2026-01-01T08:00:00+00:00",
            "valid_until": "2026-01-02T08:00:00+00:00",
            "families": {
                "eu_industrials": [
                    {
                        "point": "Industrial complex still bid",
                        "sources": ["Reuters"],
                        "source_refs": ["u1"],
                        "symbols": ["AIR.PA"],
                        "direction": "bullish",
                        "horizon": "1-10d",
                    }
                ]
            },
        }
    )
    assert brief is not None
    store.ingest_brief(brief)

    result = refresh_situation_outcomes(
        store,
        _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)}),
        families={"eu_industrials": ["AIR.PA"]},
        limit=8,
    )
    assert result["pending"] == 1
    assert result["evaluated"] == 0
    assert store.load_outcomes() == []
    assert store.load_pending_notes()[0]["verdict"] is None


def test_refresh_saute_les_immatures_en_tete_pour_juger_une_note_mature(tmp_path) -> None:
    from trader.domain.situation import NewsMacroBrief
    from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore

    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    immature = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-01-01T08:00:00+00:00|EU|weeks",
            "venue": "EU",
            "as_of": "2026-01-01T08:00:00+00:00",
            "valid_until": "2026-01-02T08:00:00+00:00",
            "symbols": {
                "AIR.PA": [
                    {
                        "point": "Long-horizon industrial bid",
                        "sources": ["Reuters"],
                        "source_refs": ["u1"],
                        "symbols": ["AIR.PA"],
                        "direction": "bullish",
                        "horizon": "trimestre",
                    }
                ]
            },
        }
    )
    mature = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-01-01T08:01:00+00:00|EU|session",
            "venue": "EU",
            "as_of": "2026-01-01T08:00:00+00:00",
            "valid_until": "2026-01-02T08:00:00+00:00",
            "symbols": {
                "MC.PA": [
                    {
                        "point": "Near-term luxury bounce",
                        "sources": ["Reuters"],
                        "source_refs": ["u2"],
                        "symbols": ["MC.PA"],
                        "direction": "bullish",
                        "horizon": "session",
                    }
                ]
            },
        }
    )
    assert immature is not None and mature is not None
    store.ingest_brief(immature)
    store.ingest_brief(mature)

    result = refresh_situation_outcomes(
        store,
        _FakeSource(
            {
                "AIR.PA": _path_bars(100.0, 110.0),
                "MC.PA": _path_bars(100.0, 110.0),
            }
        ),
        families={"eu_industrials": ["AIR.PA"], "eu_luxury": ["MC.PA"]},
        limit=1,
    )
    assert result["evaluated"] == 1
    judged = store.load_outcomes()
    assert len(judged) == 1
    assert judged[0]["section_name"] == "MC.PA"
    still_pending = {row["section_name"] for row in store.load_pending_notes()}
    assert "AIR.PA" in still_pending
