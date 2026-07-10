import json

from trader.infrastructure.state_db.global_family_board_store import (
    GlobalFamilyBoardStore,
)


def _board(board_id: str, as_of: str) -> dict:
    return {
        "schema_version": 1,
        "board_id": board_id,
        "as_of": as_of,
        "status": "partial",
        "role": "comparative_context_not_capital_allocation",
        "coverage": {},
        "venues": {},
    }


def test_store_appends_only_material_board_changes(tmp_path) -> None:
    store = GlobalFamilyBoardStore(tmp_path / "global_family_boards")

    first, first_ref, first_changed = store.append_if_changed(
        _board("board-1", "2026-07-10T12:05:00+00:00")
    )
    same, same_ref, same_changed = store.append_if_changed(
        _board("board-1", "2026-07-10T12:10:00+00:00")
    )
    second, second_ref, second_changed = store.append_if_changed(
        _board("board-2", "2026-07-10T12:15:00+00:00")
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "global_family_boards" / "2026-07-10.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert first_changed is True
    assert same_changed is False
    assert second_changed is True
    assert same == first
    assert same_ref == first_ref
    assert second_ref["board_id"] == "board-2"
    assert [row["board_id"] for row in rows] == ["board-1", "board-2"]
    assert store.read_current() == second
