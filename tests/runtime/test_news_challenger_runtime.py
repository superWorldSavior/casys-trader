from __future__ import annotations

import json
from datetime import datetime, timezone

from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db.news_challenger_run_store import NewsChallengerRunStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.runtime.news_challenger_runtime import build_news_challenger_fn


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ranked(symbols: list[str]) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "attractiveness": 100.0 - index,
            "bias": "long",
        }
        for index, symbol in enumerate(symbols)
    ]


def test_provider_uses_optional_manual_alias_and_latest_brief_refs(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (config_dir / "symbol_names.yaml").write_text(
        "2317.TW: Hon Hai Precision Industry Co., Ltd.\n",
        encoding="utf-8",
    )
    (config_dir / "symbol_news_aliases.yaml").write_text(
        "2317.TW: [Foxconn]\n",
        encoding="utf-8",
    )
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            {
                "uuid": "u-foxconn",
                "symbol": "SPY",
                "title": "Foxconn raises guidance after quarterly results",
                "publisher": "Reuters",
                "published_at": "2026-07-10T10:00:00+00:00",
            }
        ],
    )
    radar = [f"RADAR{i:02d}.TW" for i in range(40)]
    venue_ranked = _ranked([*radar, "2317.TW"])

    provider = build_news_challenger_fn(config_dir=config_dir, state_dir=state_dir, as_of=NOW)

    candidates = provider(
        venue="TW",
        venue_ranked=venue_ranked,
        radar_symbols=set(radar),
    )

    assert [candidate["symbol"] for candidate in candidates] == ["2317.TW"]
    assert candidates[0]["candidate_source"] == "fresh_news"
    assert candidates[0]["fresh_news"]["source_refs"] == ["u-foxconn"]
    assert candidates[0]["fresh_news"]["evidence"][0]["attribution"]["method"] == "manual_alias"
    first_run_id = candidates[0]["metadata"]["candidate_run_id"]
    first_run = _read_jsonl(state_dir / "news_challenger_runs" / "2026-07-10.jsonl")[-1]
    assert first_run == {
        "archived_items_read": 1,
        "as_of": NOW.isoformat(),
        "candidate_run_id": first_run_id,
        "challengers": [
            {
                "publishers": ["Reuters"],
                "score": 85,
                "source_refs": ["u-foxconn"],
                "symbol": "2317.TW",
                "valid_until": "2026-07-13T10:00:00+00:00",
            }
        ],
        "coverage_status": "partial",
        "eligible_items": 1,
        "eligible_symbol_count": 41,
        "items_read": 1,
        "radar_symbol_count": 40,
        "reason": "selection_complete",
        "rejection_counts": {},
        "schema_version": 1,
        "status": "success",
        "venue": "TW",
    }

    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "brief-tw",
            "venue": "TW",
            "as_of": "2026-07-10T11:00:00+00:00",
            "valid_until": "2026-07-11T07:00:00+00:00",
            "input_refs": {"news_item_uuids": ["u-foxconn"]},
        }
    )
    assert brief is not None
    NewsMacroBriefStore(state_dir / "news_briefs").append(brief)

    refreshed = build_news_challenger_fn(config_dir=config_dir, state_dir=state_dir, as_of=NOW)

    assert refreshed(venue="TW", venue_ranked=venue_ranked, radar_symbols=set(radar)) == []
    runs = _read_jsonl(state_dir / "news_challenger_runs" / "2026-07-10.jsonl")
    assert len(runs) == 2
    assert runs[-1]["candidate_run_id"] != first_run_id
    assert runs[-1]["status"] == "success"
    assert runs[-1]["reason"] == "selection_complete"
    assert runs[-1]["rejection_counts"] == {"already_seen": 1}
    assert json.loads(
        (state_dir / "news_challenger_runs" / "latest-TW.json").read_text(encoding="utf-8")
    ) == runs[-1]


def test_provider_returns_every_qualified_challenger_without_total_cap(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    challenger_symbols = [f"C{i:02d}" for i in range(15)]
    (config_dir / "symbol_names.yaml").write_text(
        "".join(f"{symbol}: Candidate {index} Corporation\n" for index, symbol in enumerate(challenger_symbols)),
        encoding="utf-8",
    )
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            {
                "uuid": f"u-{index}",
                "symbol": "SPY",
                "title": f"Candidate {index} raises guidance after quarterly results",
                "publisher": "Reuters",
                "published_at": f"2026-07-10T{10 - index % 3:02d}:00:00+00:00",
            }
            for index in range(len(challenger_symbols))
        ],
    )
    radar = [f"R{i:02d}" for i in range(40)]
    venue_ranked = _ranked([*radar, *challenger_symbols])
    provider = build_news_challenger_fn(config_dir=config_dir, state_dir=state_dir, as_of=NOW)

    candidates = provider(venue="US", venue_ranked=venue_ranked, radar_symbols=set(radar))

    assert {candidate["symbol"] for candidate in candidates} == set(challenger_symbols)
    assert len(candidates) == 15
    run_ids = {candidate["metadata"]["candidate_run_id"] for candidate in candidates}
    assert len(run_ids) == 1
    run = _read_jsonl(state_dir / "news_challenger_runs" / "2026-07-10.jsonl")[-1]
    assert run["candidate_run_id"] == run_ids.pop()
    assert len(run["challengers"]) == 15
    assert {item["symbol"] for item in run["challengers"]} == set(challenger_symbols)


def test_provider_is_fail_safe_on_malformed_inputs(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "symbol_names.yaml").write_text("[not a mapping]", encoding="utf-8")
    _write_jsonl(state_dir / "news_items" / "2026-07-10.jsonl", [{"broken": True}])

    provider = build_news_challenger_fn(config_dir=config_dir, state_dir=state_dir, as_of=NOW)

    assert provider(venue="US", venue_ranked=_ranked(["AAPL"]), radar_symbols={"AAPL"}) == []
    run = _read_jsonl(state_dir / "news_challenger_runs" / "2026-07-10.jsonl")[-1]
    assert run["status"] == "degraded"
    assert run["reason"] == "missing_symbol_names"
    assert run["archived_items_read"] == 1
    assert run["items_read"] == 0


def test_provider_traces_no_news_and_no_eligible_symbols(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (config_dir / "symbol_names.yaml").write_text("AAPL: Apple Inc.\n", encoding="utf-8")

    no_news = build_news_challenger_fn(config_dir=config_dir, state_dir=state_dir, as_of=NOW)
    assert no_news(venue="US", venue_ranked=_ranked(["AAPL"]), radar_symbols={"AAPL"}) == []
    assert no_news.candidate_run_ids["US"]

    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            {
                "uuid": "u-apple",
                "symbol": "AAPL",
                "title": "Apple raises guidance after quarterly results",
                "publisher": "Reuters",
                "published_at": "2026-07-10T10:00:00+00:00",
            }
        ],
    )
    no_eligible = build_news_challenger_fn(config_dir=config_dir, state_dir=state_dir, as_of=NOW)
    assert no_eligible(venue="US", venue_ranked=[], radar_symbols=set()) == []

    runs = _read_jsonl(state_dir / "news_challenger_runs" / "2026-07-10.jsonl")
    assert [(run["status"], run["reason"]) for run in runs] == [
        ("degraded", "no_archived_news_items"),
        ("degraded", "no_eligible_symbols"),
    ]
    assert runs[0]["eligible_symbol_count"] == 1
    assert runs[1]["archived_items_read"] == 1


def test_provider_traces_selector_errors_without_breaking_rotation(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (config_dir / "symbol_names.yaml").write_text("AAPL: Apple Inc.\n", encoding="utf-8")
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            {
                "uuid": "u-apple",
                "symbol": "AAPL",
                "title": "Apple raises guidance after quarterly results",
                "publisher": "Reuters",
                "published_at": "2026-07-10T10:00:00+00:00",
            }
        ],
    )

    def broken_selector(*_args, **_kwargs):
        raise RuntimeError("boom")

    provider = build_news_challenger_fn(
        config_dir=config_dir,
        state_dir=state_dir,
        as_of=NOW,
        select_fn=broken_selector,
    )

    assert provider(venue="US", venue_ranked=_ranked(["AAPL"]), radar_symbols=set()) == []
    run = _read_jsonl(state_dir / "news_challenger_runs" / "2026-07-10.jsonl")[-1]
    assert run["status"] == "error"
    assert run["reason"] == "selector_error:RuntimeError"
    assert run["challengers"] == []


def test_run_store_is_corruption_tolerant_and_keeps_latest_per_venue(tmp_path) -> None:
    base_dir = tmp_path / "news_challenger_runs"
    base_dir.mkdir()
    (base_dir / "2026-07-10.jsonl").write_text("{broken json", encoding="utf-8")
    store = NewsChallengerRunStore(base_dir)
    eu_run = {
        "candidate_run_id": "eu-run",
        "as_of": NOW.isoformat(),
        "venue": "EU",
        "status": "success",
    }
    tw_run = {
        "candidate_run_id": "tw-run",
        "as_of": NOW.isoformat(),
        "venue": "TW",
        "status": "degraded",
    }

    store.append(eu_run)
    store.append(tw_run)

    assert store.read_latest("EU") == eu_run
    assert store.read_latest("TW") == tw_run
    (base_dir / "latest-EU.json").write_text("not json", encoding="utf-8")
    assert store.read_latest("EU") == eu_run


def test_observability_store_failure_never_suppresses_challengers(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (config_dir / "symbol_names.yaml").write_text("AAPL: Apple Inc.\n", encoding="utf-8")
    _write_jsonl(
        state_dir / "news_items" / "2026-07-10.jsonl",
        [
            {
                "uuid": "u-apple",
                "symbol": "SPY",
                "title": "Apple raises guidance after quarterly results",
                "publisher": "Reuters",
                "published_at": "2026-07-10T10:00:00+00:00",
            }
        ],
    )

    class BrokenStore:
        def append(self, _record) -> None:
            raise OSError("disk full")

    provider = build_news_challenger_fn(
        config_dir=config_dir,
        state_dir=state_dir,
        as_of=NOW,
        run_store=BrokenStore(),
    )

    candidates = provider(venue="US", venue_ranked=_ranked(["SPY", "AAPL"]), radar_symbols={"SPY"})

    assert [candidate["symbol"] for candidate in candidates] == ["AAPL"]
    assert candidates[0]["metadata"]["candidate_run_id"]
