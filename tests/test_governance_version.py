from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from trader.support.metadata import governance_version

ROOT = Path(__file__).resolve().parents[1]


def _valid_config() -> dict:
    return {
        "schema_version": 1,
        "process": {
            "id": "test.paper-cycle",
            "version": "0.1",
            "name": "Test paper cycle",
        },
        "runtime_binding": {
            "status": "not_integrated",
            "note": "Test-only contract.",
        },
        "instance_contract": {
            "trigger": "A symbol becomes due.",
            "work_object": "One due symbol.",
            "instance_key": "process_instance_id",
            "process_instance_id": "Stable while the work object remains open.",
            "runtime_run_id": "Fixed identity of one daemon process after PID claim.",
            "attempt_id": "One cycle or recovery attempt for an open instance.",
        },
        "boundary": {
            "start": "The work object is due.",
            "result": "A correlated decision and its effects.",
            "end": "A terminal predicate is proved.",
            "excluded": ["Research production"],
        },
        "accountability": {
            "status": "unassigned",
            "role": None,
            "gap": "No named accountable role.",
        },
        "completion_contract": {
            "terminal_outcomes": {
                name: {
                    "predicate": f"{name} predicate",
                    "authoritative_state": f"{name} source of truth",
                    "required_evidence": [f"{name} evidence"],
                }
                for name in ("completed", "failed", "escalated", "cancelled")
            },
            "non_terminal_states": {
                "recovery_required": {
                    "predicate": "The committing effect remains unknown.",
                    "authoritative_state": "Execution task and broker state.",
                    "required_action": "Reconcile before selecting a terminal outcome.",
                }
            },
        },
    }


def _write_fixture_root(root: Path, *, include_guardrails: bool = True) -> None:
    for relative_path in governance_version.REQUIRED_ARTIFACT_PATHS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative_path == governance_version.PROCESS_CONFIG_PATH:
            path.write_text(yaml.safe_dump(_valid_config(), sort_keys=False), encoding="utf-8")
        else:
            path.write_text(f"fixture for {relative_path}\n", encoding="utf-8")
    if include_guardrails:
        path = root / governance_version.OPTIONAL_ARTIFACT_PATHS[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]\n", encoding="utf-8")


def _write_config(root: Path, config: dict) -> None:
    path = root / governance_version.PROCESS_CONFIG_PATH
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def test_repository_governance_contract_is_valid_and_integrated_by_default() -> None:
    config = governance_version.load_process_governance(ROOT)
    version = governance_version.current_governance_version(ROOT)

    assert config["process"]["id"] == "casys-trader.paper-decision-cycle"
    assert config["runtime_binding"]["status"] == "integrated"
    assert version["runtime_binding_status"] == "integrated"
    assert config["accountability"] == {
        "status": "assigned",
        "role": "Casys Trader Process Owner",
        "holder": "Erwan Lee Pesle",
        "accepted_on": "2026-08-09",
    }
    assert version["accountability"] == config["accountability"]
    assert "recovery_required" not in config["completion_contract"]["terminal_outcomes"]
    assert "recovery_required" in config["completion_contract"]["non_terminal_states"]
    assert len(version["bundle_sha256"]) == 64
    assert {artifact["path"] for artifact in version["artifacts"]} == set(
        governance_version.REQUIRED_ARTIFACT_PATHS + governance_version.OPTIONAL_ARTIFACT_PATHS
    )


def test_current_governance_version_hashes_only_the_fixed_allowlist(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    secret = tmp_path / ".env"
    secret.write_text("API_TOKEN=first-secret\n", encoding="utf-8")

    first = governance_version.current_governance_version(tmp_path)
    secret.write_text("API_TOKEN=second-secret\n", encoding="utf-8")
    second = governance_version.current_governance_version(tmp_path)

    expected_paths = set(governance_version.REQUIRED_ARTIFACT_PATHS) | set(governance_version.OPTIONAL_ARTIFACT_PATHS)
    assert {artifact["path"] for artifact in first["artifacts"]} == expected_paths
    assert all(".env" not in artifact["path"] for artifact in first["artifacts"])
    assert first["bundle_sha256"] == second["bundle_sha256"]
    assert (
        first["bundle_sha256"]
        == hashlib.sha256(
            json.dumps(
                [{"path": artifact["path"], "sha256": artifact["sha256"]} for artifact in first["artifacts"]],
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )


def test_governance_bundle_changes_when_an_allowlisted_artifact_changes(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    first = governance_version.current_governance_version(tmp_path)

    mandate_path = tmp_path / "mandate/mandate.md"
    mandate_path.write_text("changed mandate\n", encoding="utf-8")
    second = governance_version.current_governance_version(tmp_path)

    assert first["bundle_sha256"] != second["bundle_sha256"]
    first_hashes = {artifact["path"]: artifact["sha256"] for artifact in first["artifacts"]}
    second_hashes = {artifact["path"]: artifact["sha256"] for artifact in second["artifacts"]}
    assert first_hashes["mandate/mandate.md"] != second_hashes["mandate/mandate.md"]


def test_optional_guardrails_are_reported_without_failing(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path, include_guardrails=False)

    version = governance_version.current_governance_version(tmp_path)

    assert version["missing_optional_artifacts"] == ["mandate/guardrails.json"]
    assert "mandate/guardrails.json" not in {artifact["path"] for artifact in version["artifacts"]}


def test_recovery_required_cannot_be_declared_terminal(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    config = copy.deepcopy(_valid_config())
    config["completion_contract"]["terminal_outcomes"]["recovery_required"] = {
        "predicate": "Wrongly terminal.",
        "authoritative_state": "None.",
        "required_evidence": ["None."],
    }
    _write_config(tmp_path, config)

    with pytest.raises(
        governance_version.GovernanceConfigError,
        match="recovery_required must never be terminal",
    ):
        governance_version.load_process_governance(tmp_path)


def test_unassigned_accountability_cannot_name_a_role(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    config = copy.deepcopy(_valid_config())
    config["accountability"]["role"] = "process owner"
    _write_config(tmp_path, config)

    with pytest.raises(
        governance_version.GovernanceConfigError,
        match="role, holder and accepted_on must be null",
    ):
        governance_version.load_process_governance(tmp_path)


def test_assigned_accountability_requires_holder_and_iso_acceptance_date(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    config = copy.deepcopy(_valid_config())
    config["accountability"] = {
        "status": "assigned",
        "role": "Process Owner",
        "holder": "",
        "accepted_on": "09/08/2026",
    }
    _write_config(tmp_path, config)

    with pytest.raises(
        governance_version.GovernanceConfigError,
        match="accountability.holder",
    ):
        governance_version.load_process_governance(tmp_path)

    config["accountability"]["holder"] = "Operator"
    _write_config(tmp_path, config)
    with pytest.raises(
        governance_version.GovernanceConfigError,
        match="accountability.accepted_on must be an ISO date",
    ):
        governance_version.load_process_governance(tmp_path)


def test_missing_required_artifact_fails_closed(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    (tmp_path / "config/risk.yaml").unlink()

    with pytest.raises(
        governance_version.GovernanceConfigError,
        match="required governance artifact is missing: config/risk.yaml",
    ):
        governance_version.current_governance_version(tmp_path)


def test_allowlisted_symlink_is_rejected(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    prompt_path = tmp_path / "trader/agent/protocol/prompts.py"
    prompt_path.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret"
    outside.write_text("secret\n", encoding="utf-8")
    prompt_path.symlink_to(outside)

    try:
        with pytest.raises(
            governance_version.GovernanceConfigError,
            match="cannot be a symlink",
        ):
            governance_version.current_governance_version(tmp_path)
    finally:
        outside.unlink()
