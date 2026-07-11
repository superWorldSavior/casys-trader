import json

from trader.infrastructure.state_db.global_situation_digest_store import GlobalSituationDigestStore


def _digest(as_of: str, point: str) -> dict:
    return {
        "as_of": as_of,
        "regime": "mixed",
        "rates_bias": "neutral",
        "usd_bias": "neutral",
        "points": [{"point": point, "sources": ["Reuters"], "source_refs": ["ref"]}],
        "source_refs": ["ref"],
        "coverage": {"venues_seen": ["EU", "US"], "point_count": 1},
    }


def test_store_appends_only_material_digest_changes(tmp_path) -> None:
    store = GlobalSituationDigestStore(tmp_path / "global_situation_digests")

    first, first_ref, first_changed = store.append_if_changed(
        _digest("2026-07-10T12:00:00+00:00", "Rates remain restrictive.")
    )
    same, same_ref, same_changed = store.append_if_changed(
        _digest("2026-07-10T12:00:00+00:00", "Rates remain restrictive.")
    )
    second, second_ref, second_changed = store.append_if_changed(
        _digest("2026-07-10T12:00:00+00:00", "Dollar momentum weakens.")
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "global_situation_digests" / "2026-07-10.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert first_changed is True
    assert same_changed is False
    assert second_changed is True
    assert same == first
    assert same_ref == first_ref
    assert second_ref["digest_id"] == second["digest_id"]
    assert [row["digest_id"] for row in rows] == [first["digest_id"], second["digest_id"]]
    assert store.read_current() == second
