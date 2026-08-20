from __future__ import annotations

from copy import deepcopy

from trader.support.metadata.experiment import (
    ID_PREFIX,
    active_model_preset,
    build_experiment_context,
    decision_experiment,
    inherited_experiment,
)


def _risk_policy() -> dict:
    return {
        "max_position_value": 50_000,
        "max_gross_exposure": 100_000,
        "max_order_value": 50_000,
        "min_equity": 50_000,
        "max_risk_per_trade_pct": 0.01,
        "min_trade_confidence": 0.7,
        "full_risk_confidence": 0.9,
        "confidence_gate_enabled": False,
        "require_hard_stop": False,
    }


def _context(*, dirty: bool = False, commission_model: str = "ibkr") -> dict:
    return build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": dirty,
            "git_tracked_dirty": dirty,
        },
        model_preset="codex-luna-medium",
        risk_policy=_risk_policy(),
        commission_model=commission_model,
    )


def test_active_model_preset_reads_only_generated_marker(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SECRET_TOKEN=do-not-persist\n"
        "# >>> casys:model-preset=codex-luna-medium >>>\n"
        "TRADER_MODEL=gpt-5.6-luna\n",
        encoding="utf-8",
    )

    assert active_model_preset(env_path) == "codex-luna-medium"


def test_experiment_id_is_stable_and_canonical() -> None:
    first = decision_experiment(
        _context(), provider="acpx", model="gpt-5.6-luna"
    )
    reordered_risk = dict(reversed(list(_risk_policy().items())))
    second_context = build_experiment_context(
        code_version={
            "git_dirty": False,
            "git_tracked_dirty": False,
            "git_commit": "a" * 40,
        },
        model_preset="codex-luna-medium",
        risk_policy=reordered_risk,
        commission_model="ibkr",
    )
    second = decision_experiment(
        second_context,
        provider="acpx",
        model="gpt-5.6-luna",
    )

    assert first["experiment_id"] == second["experiment_id"]
    assert first["experiment_id"].startswith(ID_PREFIX)
    assert first["decision_grade"] is True


def test_experiment_id_changes_with_provider_model_risk_or_commission() -> None:
    baseline = decision_experiment(
        _context(), provider="acpx", model="gpt-5.6-luna"
    )["experiment_id"]
    fallback = decision_experiment(
        _context(), provider="acpx-claude-sonnet", model="sonnet"
    )["experiment_id"]
    changed_risk = _risk_policy()
    changed_risk["max_risk_per_trade_pct"] = 0.005
    risk_context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": False,
            "git_tracked_dirty": False,
        },
        model_preset="codex-luna-medium",
        risk_policy=changed_risk,
        commission_model="ibkr",
    )
    risk_id = decision_experiment(
        risk_context, provider="acpx", model="gpt-5.6-luna"
    )["experiment_id"]
    no_fee = decision_experiment(
        _context(commission_model="none"),
        provider="acpx",
        model="gpt-5.6-luna",
    )["experiment_id"]

    assert len({baseline, fallback, risk_id, no_fee}) == 4


def test_dirty_or_incomplete_context_fails_closed_without_reusable_id() -> None:
    dirty = decision_experiment(
        _context(dirty=True), provider="acpx", model="gpt-5.6-luna"
    )
    missing_model = decision_experiment(
        _context(), provider="acpx", model=None
    )
    incomplete = deepcopy(_context())
    incomplete["risk"].pop("require_hard_stop")
    missing_risk = decision_experiment(
        incomplete, provider="acpx", model="gpt-5.6-luna"
    )

    assert dirty["experiment_id"] is None
    assert "git_tracked_dirty:working_tree_not_clean" in dirty["issues"]
    assert dirty["decision_grade"] is False
    assert missing_model["experiment_id"] is None
    assert "model.model:missing" in missing_model["issues"]
    assert missing_risk["experiment_id"] is None
    assert "risk:incomplete" in missing_risk["issues"]


def test_untracked_files_do_not_invalidate_a_clean_commit_cohort() -> None:
    context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": True,
            "git_tracked_dirty": False,
            "git_dirty_files": ["?? prompt_0.txt"],
        },
        model_preset="codex-luna-medium",
        risk_policy=_risk_policy(),
        commission_model="ibkr",
    )

    result = decision_experiment(
        context, provider="acpx", model="gpt-5.6-luna"
    )

    assert result["experiment_id"] is not None
    assert result["components"]["git_tracked_dirty"] is False
    assert "prompt_0.txt" not in str(result)


def test_inherited_experiment_revalidates_persisted_hash() -> None:
    original = decision_experiment(
        _context(), provider="acpx", model="gpt-5.6-luna"
    )
    tampered = deepcopy(original)
    tampered["components"]["risk"]["max_order_value"] = 999_999.0

    assert inherited_experiment(original) == original
    assert inherited_experiment(tampered) is None
