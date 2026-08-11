from __future__ import annotations

import json
from pathlib import Path

from trader.infrastructure.llm.acpx_backend import _run_one_shot_command


def test_subprocess_codex_desactive_apps_plugins_et_tous_les_skills(monkeypatch, tmp_path) -> None:
    codex_home = tmp_path / "codex-home"
    system_skill = codex_home / "skills" / ".system" / "openai-docs" / "SKILL.md"
    external_skill = tmp_path / ".agents" / "skills" / "acpx" / "SKILL.md"
    system_skill.parent.mkdir(parents=True)
    external_skill.parent.mkdir(parents=True)
    system_skill.write_text("system", encoding="utf-8")
    external_skill.write_text("external", encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_CONFIG", raising=False)
    captured: dict = {}

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, _command, **kwargs):
            captured.update(kwargs)

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "trader.infrastructure.llm.acpx_backend._terminate_process_group",
        lambda _pid: None,
    )

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    child_env = captured["env"]
    assert Path(child_env["CODEX_HOME"]) == codex_home
    config = json.loads(child_env["CODEX_CONFIG"])
    assert config["features"]["apps"] is False
    assert config["features"]["multi_agent"] is False
    assert config["features"]["plugins"] is False
    assert config["features"]["recommended_plugins"] is False
    assert config["agents"]["enabled"] is False
    assert config["project_doc_max_bytes"] == 0
    assert "pure JSON object" in config["developer_instructions"]
    disabled = {
        Path(item["path"])
        for item in config["skills"]["config"]
        if item["enabled"] is False
    }
    # Codex CLI 0.147 applique l'override au chemin SKILL.md exact. Un test
    # prompt-input A/B couvre ce comportement de version dans la référence.
    assert system_skill.resolve() in disabled
    assert external_skill.resolve() in disabled
