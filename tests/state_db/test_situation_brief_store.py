import json
from datetime import datetime, timezone

from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore


def _brief(
    point: str,
    *,
    venue: str = "EU",
    as_of: str = "2026-07-09T07:00:00+00:00",
    valid_until: str = "2026-07-10T07:00:00+00:00",
) -> NewsMacroBrief:
    payload = {
        "brief_id": f"{as_of}|{venue}",
        "venue": venue,
        "as_of": as_of,
        "valid_until": valid_until,
        "zones": {"EU": [{"point": point, "sources": ["uuid-1"], "severity": "watch"}]},
    }
    brief = NewsMacroBrief.from_mapping(payload)
    assert brief is not None
    return brief


def test_news_macro_brief_store_appends_jsonl_and_latest_ref(tmp_path) -> None:
    store = NewsMacroBriefStore(tmp_path / "news_briefs")
    brief = _brief("ECB repricing pressure")

    ref = store.append(brief)

    day_file = tmp_path / "news_briefs" / "2026-07-09.jsonl"
    latest_file = tmp_path / "news_briefs" / "latest-EU.jsonl"
    day_rows = [json.loads(line) for line in day_file.read_text(encoding="utf-8").splitlines()]
    latest_rows = [json.loads(line) for line in latest_file.read_text(encoding="utf-8").splitlines()]

    assert len(day_rows) == 1
    assert len(latest_rows) == 1
    assert day_rows[0]["venue"] == "EU"
    assert latest_rows[0]["brief_id"] == brief.brief_id
    assert store.read("2026-07-09", venue="EU") == brief
    assert ref == {
        "date": "2026-07-09",
        "venue": "EU",
        "brief_id": "2026-07-09T07:00:00+00:00|EU",
        "as_of": "2026-07-09T07:00:00+00:00",
    }


def test_news_macro_brief_store_never_replaces_canonical_jsonl(tmp_path) -> None:
    store = NewsMacroBriefStore(tmp_path / "news_briefs")

    first = _brief("first")
    second = _brief("second", as_of="2026-07-09T08:00:00+00:00")
    store.append(first)
    store.append(second)

    rows = [
        json.loads(line)
        for line in (tmp_path / "news_briefs" / "2026-07-09.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    latest = store.read_latest("EU")

    assert [row["zones"]["EU"][0]["point"] for row in rows] == ["first", "second"]
    assert latest == second
    assert store.active_ref("EU", at=datetime(2026, 7, 9, 8, 30, tzinfo=timezone.utc)) == {
        "date": "2026-07-09",
        "venue": "EU",
        "brief_id": "2026-07-09T08:00:00+00:00|EU",
        "as_of": "2026-07-09T08:00:00+00:00",
    }


def test_news_macro_brief_store_returns_none_for_corrupt_jsonl_lines(tmp_path) -> None:
    base = tmp_path / "news_briefs"
    base.mkdir()
    (base / "2026-07-09.jsonl").write_text("{bad json\n[]\n", encoding="utf-8")

    assert NewsMacroBriefStore(base).read("2026-07-09") is None


def test_news_macro_brief_latest_stays_single_jsonl_line_and_leaves_no_tmp(tmp_path) -> None:
    store = NewsMacroBriefStore(tmp_path / "news_briefs")
    store.append(_brief("atomic latest"))

    latest = tmp_path / "news_briefs" / "latest-EU.jsonl"
    text = latest.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert "\n  " not in text
    assert json.loads(text)["venue"] == "EU"
    assert list((tmp_path / "news_briefs").glob("*.tmp")) == []
