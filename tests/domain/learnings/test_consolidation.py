from datetime import datetime, timedelta, timezone

from trader.domain.learnings.consolidation import (
    curation_due_reason,
    new_rule_id,
    should_keep_omitted_rule,
    stable_rule_id,
)
from trader.domain.learnings.selection import select_new_raw


def test_stable_rule_id_is_deterministic() -> None:
    assert stable_rule_id("  Hello  World ") == stable_rule_id("hello world")


def test_new_rule_id_suffixes_collisions() -> None:
    first = stable_rule_id("same")
    assert new_rule_id("same", used_ids=set()) == first
    assert new_rule_id("same", used_ids={first}) == f"{first}_2"


def test_select_new_raw_filtre_par_watermark() -> None:
    rows = [
        {"ts": "2026-06-08T10:00:00+00:00", "note": "old"},
        {"ts": "2026-06-08T10:30:00+00:00", "note": "same"},
        {"ts": "2026-06-08T11:00:00+00:00", "note": "new"},
    ]
    selected = select_new_raw(rows, watermark="2026-06-08T10:30:00+00:00")
    assert [item["note"] for item in selected] == ["new"]


def test_curation_due_reasons() -> None:
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    assert curation_due_reason({"new": 50, "feedback": 0}, threshold=50, now=now) == "new_threshold"
    assert curation_due_reason({"new": 1, "feedback": 10}, threshold=50, now=now) == "feedback_threshold"
    assert (
        curation_due_reason(
            {
                "new": 1,
                "feedback": 0,
                "oldest_changed_at": (now - timedelta(days=2)).isoformat(),
            },
            threshold=50,
            now=now,
        )
        == "daily_catch_up"
    )
    assert curation_due_reason({"new": 1, "feedback": 0}, threshold=50, now=now) is None


def test_keep_measured_or_young_rules() -> None:
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    measured = {"memrl": {"q_updates": 10, "created_at": "2026-01-01T00:00:00+00:00"}}
    young = {"memrl": {"q_updates": 0, "created_at": (now - timedelta(days=1)).isoformat()}}
    old = {"memrl": {"q_updates": 0, "created_at": (now - timedelta(days=10)).isoformat()}}
    assert should_keep_omitted_rule(measured, now=now) is True
    assert should_keep_omitted_rule(young, now=now) is True
    assert should_keep_omitted_rule(old, now=now) is False
