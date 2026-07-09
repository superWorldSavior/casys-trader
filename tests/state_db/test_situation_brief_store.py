import json

from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore


def _brief(point: str, *, as_of: str = "2026-07-09T07:00:00+00:00") -> NewsMacroBrief:
    payload = {
        "as_of": as_of,
        "valid_until": "2026-07-10T07:00:00+00:00",
        "zones": {"US": [{"point": point, "sources": ["uuid-1"], "severity": "watch"}]},
    }
    brief = NewsMacroBrief.from_mapping(payload)
    assert brief is not None
    return brief


def test_news_macro_brief_store_write_read_and_ref(tmp_path) -> None:
    store = NewsMacroBriefStore(tmp_path / "news_briefs")
    brief = _brief("Fed repricing pressure")

    store.write(brief)

    saved = store.read("2026-07-09")
    assert saved == brief
    assert store.active_ref("2026-07-09") == {
        "date": "2026-07-09",
        "as_of": "2026-07-09T07:00:00+00:00",
    }


def test_news_macro_brief_store_archives_replaced_payload(tmp_path) -> None:
    store = NewsMacroBriefStore(tmp_path / "news_briefs")

    store.write(_brief("first"))
    store.write(_brief("second"))

    history = tmp_path / "news_briefs-history.jsonl"
    rows = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["path"] == "2026-07-09.json"
    assert rows[0]["payload"]["zones"]["US"][0]["point"] == "first"
    assert rows[0]["replaced_by"] == {
        "date": "2026-07-09",
        "as_of": "2026-07-09T07:00:00+00:00",
    }


def test_news_macro_brief_store_returns_none_for_corrupt_payload(tmp_path) -> None:
    base = tmp_path / "news_briefs"
    base.mkdir()
    (base / "2026-07-09.json").write_text("{bad json", encoding="utf-8")

    assert NewsMacroBriefStore(base).read("2026-07-09") is None
