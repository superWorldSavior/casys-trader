import json

from trader.application.strategy_language_migration import (
    migrate_jsonl_file,
    migrate_strategy_language_value,
)


def test_migrate_strategy_language_value_renomme_outils_intents_et_exit_update() -> None:
    row = {
        "symbol": "SPY",
        "intent": "REVERSE",
        "rationale": "add_without_position",
        "runtime": {
            "amend_exit": {"hard_stop": 97.0},
            "amend_exit_applied": False,
            "amend_exit_reason": "amend_exit_conflicts_with_propose_order",
            "tool_calls": [
                {
                    "tool": "propose_order",
                    "args": {
                        "intent": "OPEN_LONG",
                        "qty": 3,
                        "exit_plan": {"hard_stop": 97.0},
                    },
                },
                {"tool": "propose_order", "args": {"intent": "REDUCE", "fraction": 0.5}},
                {"tool": "amend_exit", "args": {"stop": 95.0}},
            ],
        },
    }

    migrated = migrate_strategy_language_value(row)

    assert migrated["intent"] == "FLIP"
    assert migrated["rationale"] == "scale_in_without_position"
    assert migrated["runtime"]["exit_update"] == {"hard_stop": 97.0}
    assert migrated["runtime"]["exit_update_applied"] is False
    assert migrated["runtime"]["exit_update_reason"] == "strategy_exit_conflicts_with_strategy_entry"
    calls = migrated["runtime"]["tool_calls"]
    assert calls == [
        {"tool": "strategy_entry", "args": {"qty": 3, "exit": {"hard_stop": 97.0}, "direction": "long"}},
        {"tool": "strategy_close", "args": {"qty_percent": 50.0}},
        {
            "tool": "strategy_exit",
            "args": {"stop": 95.0},
            "detail": {
                "reason": "strategy_exit_conflicts_with_strategy_entry",
                "exit_update_applied": False,
            },
        },
    ]
    assert migrate_strategy_language_value(migrated) == migrated


def test_migrate_jsonl_file_est_idempotent(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        json.dumps({"intent": "ADD", "runtime": {"tool_calls": [{"tool": "exit_update", "args": {}}]}})
        + "\n",
        encoding="utf-8",
    )

    assert migrate_jsonl_file(path) == 1
    assert migrate_jsonl_file(path) == 0
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row == {"intent": "SCALE_IN", "runtime": {"tool_calls": [{"tool": "strategy_exit", "args": {}}]}}


def test_migrate_strategy_language_value_enrichit_audit_exit_update_runtime() -> None:
    row = {
        "decision": {
            "exit_update_applied": False,
            "exit_update_reason": "resolve_failed:hard_stop_structural_wrong_side",
            "tool_calls": [{"tool": "strategy_exit", "outcome": "rejected", "detail": {}}],
        },
        "runtime": {
            "exit_update": True,
            "tool_calls": [{"tool": "strategy_exit", "outcome": "rejected", "detail": {}}],
        },
    }

    migrated = migrate_strategy_language_value(row)

    assert migrated["runtime"]["exit_update_applied"] is False
    assert migrated["runtime"]["exit_update_reason"] == "resolve_failed:hard_stop_structural_wrong_side"
    assert migrated["runtime"]["tool_calls"][0]["detail"] == {
        "reason": "resolve_failed:hard_stop_structural_wrong_side",
        "exit_update_applied": False,
    }
    assert migrated["decision"]["tool_calls"][0]["detail"] == {
        "reason": "resolve_failed:hard_stop_structural_wrong_side",
        "exit_update_applied": False,
    }
    assert migrate_strategy_language_value(migrated) == migrated
