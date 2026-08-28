from __future__ import annotations

import json
import os
from copy import deepcopy

from trader.agent.llm import build_default_router_from_env
from trader.infrastructure.llm.acpx_backend import AcpxBackend
from trader.infrastructure.llm.cursor_backend import CursorAgentBackend
from trader.infrastructure.llm.openai_backend import OpenAICompatibleBackend
from trader.support.metadata.experiment import (
    ID_PREFIX,
    LEGACY_ID_PREFIX,
    V2_ID_PREFIX,
    _experiment_id,
    active_model_preset,
    build_experiment_context,
    decision_experiment,
    inherited_experiment,
    model_profiles_from_backends,
)


def _write_acpx_entrypoint(path, *, marker: str = "v1"):
    path.write_text(f"#!/bin/sh\n# {marker}\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _configure_acpx_agents(monkeypatch, tmp_path, agents: dict[str, list[str]]) -> None:
    home = tmp_path / "home"
    config_dir = home / ".acpx"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"agents": {name: {"argv": argv} for name, argv in agents.items()}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


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


def _model_profiles(*, agent: str = "codex", reasoning_effort: str = "medium") -> dict:
    return {
        "acpx": {
            "configured_model": "gpt-5.6-luna",
            "transport": "acpx",
            "agent": agent,
            "reasoning_effort": reasoning_effort,
            "profile_fingerprint": "sha256:" + "b" * 64,
        },
        "acpx-claude-sonnet": {
            "configured_model": "sonnet",
            "transport": "acpx",
            "agent": "claude",
            "reasoning_effort": "provider-default",
            "profile_fingerprint": "sha256:" + "b" * 64,
        },
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
        model_profiles=_model_profiles(),
    )


def test_active_model_preset_reads_only_generated_marker(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SECRET_TOKEN=do-not-persist\n# >>> casys:model-preset=codex-luna-medium >>>\nTRADER_MODEL=gpt-5.6-luna\n",
        encoding="utf-8",
    )

    assert active_model_preset(env_path) == "codex-luna-medium"


def test_model_profile_fingerprint_is_non_secret_and_causally_scoped(monkeypatch, tmp_path) -> None:
    profile_dir = tmp_path / "codex-home"
    profile_dir.mkdir()
    config_path = profile_dir / "config.toml"
    config_path.write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\npersonality = "pragmatic"\napi_key = "secret-one"\n',
        encoding="utf-8",
    )
    agent_bin = _write_acpx_entrypoint(tmp_path / "codex-agent")
    _configure_acpx_agents(monkeypatch, tmp_path, {"codex": [str(agent_bin)]})
    backend = AcpxBackend(
        provider="acpx",
        model="gpt-5.6-luna",
        agent="codex",
        codex_home=str(profile_dir),
        reasoning_effort="medium",
        acpx_bin=str(_write_acpx_entrypoint(tmp_path / "acpx")),
    )

    first = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]
    config_path.write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\npersonality = "pragmatic"\napi_key = "secret-two"\n',
        encoding="utf-8",
    )
    secret_only_change = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]
    config_path.write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\npersonality = "friendly"\napi_key = "secret-two"\n',
        encoding="utf-8",
    )
    causal_change = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]

    assert first["reasoning_effort"] == "medium"
    assert first["profile_fingerprint"] == secret_only_change["profile_fingerprint"]
    assert first["profile_fingerprint"] != causal_change["profile_fingerprint"]
    assert "secret" not in str(first)


def test_claude_fallback_does_not_inherit_codex_reasoning_profile(monkeypatch, tmp_path) -> None:
    _configure_acpx_agents(monkeypatch, tmp_path, {})
    acpx_bin = _write_acpx_entrypoint(tmp_path / "acpx")
    backend = AcpxBackend(
        provider="acpx-claude-sonnet",
        model="sonnet",
        agent="claude",
        codex_home=str(tmp_path / "missing-codex-home"),
        acpx_bin=str(acpx_bin),
    )

    profile = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]

    assert profile["agent"] == "claude"
    assert profile["reasoning_effort"] == "provider-default"
    # ACPX's built-in Claude command is a floating semver range.  It must not
    # inherit Codex's effort, nor claim a stable adapter identity.
    assert profile["profile_fingerprint"] is None


def test_acpx_agent_adapter_fingerprint_resolves_alias_and_splits_path_or_content(
    monkeypatch,
    tmp_path,
) -> None:
    acpx_bin = _write_acpx_entrypoint(tmp_path / "acpx")
    first_bin = _write_acpx_entrypoint(tmp_path / "codex-agent-a")
    alias_bin = tmp_path / "codex-agent-alias"
    alias_bin.symlink_to(first_bin)
    copied_bin = _write_acpx_entrypoint(tmp_path / "codex-agent-b")
    profile_dir = tmp_path / "codex-home"
    profile_dir.mkdir()
    (profile_dir / "config.toml").write_text(
        'model = "gpt-5.6-luna"\nmodel_reasoning_effort = "medium"\n',
        encoding="utf-8",
    )

    def profile(agent_bin, *, secret: str = "secret-one"):
        _configure_acpx_agents(
            monkeypatch,
            tmp_path,
            {"codex": [str(agent_bin), f"--opaque-token={secret}"]},
        )
        backend = AcpxBackend(
            provider="acpx",
            model="gpt-5.6-luna",
            agent="codex",
            codex_home=str(profile_dir),
            acpx_bin=str(acpx_bin),
        )
        return model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]

    first = profile(first_bin)
    same_entrypoint = profile(alias_bin)
    secret_change = profile(alias_bin, secret="secret-two")
    different_path = profile(copied_bin)
    _write_acpx_entrypoint(first_bin, marker="v2-content")
    changed_content = profile(first_bin)

    assert first == same_entrypoint
    assert first["profile_fingerprint"] != secret_change["profile_fingerprint"]
    assert first["profile_fingerprint"] != different_path["profile_fingerprint"]
    assert first["profile_fingerprint"] != changed_content["profile_fingerprint"]
    assert str(tmp_path) not in str(first)
    assert "opaque-token" not in str(first)
    assert "secret-one" not in str(first)


def test_acpx_project_agent_override_takes_precedence_over_global(
    monkeypatch,
    tmp_path,
) -> None:
    acpx_bin = _write_acpx_entrypoint(tmp_path / "acpx")
    global_agent = _write_acpx_entrypoint(tmp_path / "global-agent")
    project_agent = _write_acpx_entrypoint(tmp_path / "project-agent")
    profile_dir = tmp_path / "codex-home"
    profile_dir.mkdir()
    (profile_dir / "config.toml").write_text(
        'model = "gpt-5.6-luna"\nmodel_reasoning_effort = "medium"\n',
        encoding="utf-8",
    )
    _configure_acpx_agents(
        monkeypatch,
        tmp_path,
        {"codex": [str(global_agent)]},
    )
    project_config = tmp_path / ".acpxrc.json"
    project_config.write_text(
        json.dumps({"agents": {"codex": {"argv": [str(project_agent)]}}}),
        encoding="utf-8",
    )
    backend = AcpxBackend(
        provider="acpx",
        model="gpt-5.6-luna",
        agent="codex",
        codex_home=str(profile_dir),
        acpx_bin=str(acpx_bin),
    )

    project_override = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]
    project_config.unlink()
    global_only = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]

    assert project_override["profile_fingerprint"] != global_only["profile_fingerprint"]


def test_cursor_cli_non_fast_est_fingerprint_avec_effort_xhigh(monkeypatch, tmp_path) -> None:
    _write_acpx_entrypoint(tmp_path / "cursor-agent")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("TRADER_ACPX_AGENT", "cursor")
    monkeypatch.setenv("TRADER_MODEL", "cursor-grok-4.6-xhigh")
    monkeypatch.setenv("TRADER_FALLBACK_ACPX_AGENT", "grok-build")
    monkeypatch.setenv("TRADER_FALLBACK_MODEL", "grok-4.6")

    router = build_default_router_from_env(env_path=None)
    profiles = model_profiles_from_backends(router.backends, repo_root=tmp_path)

    assert router.backends[0].provider == "cursor-agent"
    assert isinstance(router.backends[0], CursorAgentBackend)
    profile = profiles["cursor-agent"]
    assert profile["configured_model"] == "cursor-grok-4.6-xhigh"
    assert profile["transport"] == "cursor-cli"
    assert profile["agent"] == "cursor-agent"
    assert profile["reasoning_effort"] == "xhigh"
    assert str(profile["profile_fingerprint"]).startswith("sha256:")
    assert "acpx" not in profiles


def test_exact_npx_adapter_is_fingerprinted_but_range_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    acpx_bin = _write_acpx_entrypoint(tmp_path / "acpx")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx_bin = _write_acpx_entrypoint(bin_dir / "npx")
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ.get('PATH', '')}")
    npm_cache = tmp_path / "home" / ".npm"
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(npm_cache))
    monkeypatch.delenv("npm_config_cache", raising=False)
    profile_dir = tmp_path / "codex-home"
    profile_dir.mkdir()
    (profile_dir / "config.toml").write_text(
        'model = "gpt-5.6-luna"\nmodel_reasoning_effort = "medium"\n',
        encoding="utf-8",
    )
    _configure_acpx_agents(
        monkeypatch,
        tmp_path,
        {"codex": [str(npx_bin), "-y", "@example/codex-acp@1.2.3"]},
    )
    package_root = tmp_path / "home" / ".npm" / "_npx" / "one-resolution" / "node_modules" / "@example" / "codex-acp"
    dist_dir = package_root / "dist"
    dist_dir.mkdir(parents=True)
    entrypoint = dist_dir / "index.js"
    entrypoint.write_text("export const adapter = 'v1';\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@example/codex-acp",
                "version": "1.2.3",
                "bin": {"codex-acp": "dist/index.js"},
                "dependencies": {},
            }
        ),
        encoding="utf-8",
    )
    backend = AcpxBackend(
        provider="acpx",
        model="gpt-5.6-luna",
        agent="codex",
        codex_home=str(profile_dir),
        acpx_bin=str(acpx_bin),
    )

    exact = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]
    entrypoint.write_text("export const adapter = 'v2';\n", encoding="utf-8")
    changed_cache = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]
    _configure_acpx_agents(
        monkeypatch,
        tmp_path,
        {"codex": [str(npx_bin), "-y", "@example/codex-acp@^1.2.3"]},
    )
    floating = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx"]

    assert str(exact["profile_fingerprint"]).startswith("sha256:")
    assert exact["profile_fingerprint"] != changed_cache["profile_fingerprint"]
    assert floating["profile_fingerprint"] is None


def test_acpx_transport_fingerprint_resolves_alias_and_splits_path_or_content(
    monkeypatch,
    tmp_path,
) -> None:
    first_bin = _write_acpx_entrypoint(tmp_path / "acpx-a")
    alias_bin = tmp_path / "acpx-alias"
    alias_bin.symlink_to(first_bin)
    copied_bin = _write_acpx_entrypoint(tmp_path / "acpx-b")
    agent_bin = _write_acpx_entrypoint(tmp_path / "claude-agent")
    _configure_acpx_agents(monkeypatch, tmp_path, {"claude": [str(agent_bin)]})

    def profile(acpx_bin, *, session_label: str):
        backend = AcpxBackend(
            provider="acpx-claude-sonnet",
            model="sonnet",
            agent="claude",
            acpx_bin=str(acpx_bin),
            session_label=session_label,
        )
        return model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]

    first = profile(first_bin, session_label="runtime-a")
    same_entrypoint = profile(alias_bin, session_label="runtime-b")
    different_path = profile(copied_bin, session_label="runtime-a")
    _write_acpx_entrypoint(first_bin, marker="v2-content")
    changed_content = profile(first_bin, session_label="runtime-a")

    # The daemon's decision path supplies its own task-scoped session name, so
    # AcpxBackend.session_label is not part of the executed decision semantics.
    assert first == same_entrypoint
    assert first["profile_fingerprint"] != different_path["profile_fingerprint"]
    assert first["profile_fingerprint"] != changed_content["profile_fingerprint"]
    assert str(tmp_path) not in str(first)


def test_acpx_transport_fingerprint_includes_imported_runtime_chunks(monkeypatch, tmp_path) -> None:
    agent_bin = _write_acpx_entrypoint(tmp_path / "claude-agent")
    _configure_acpx_agents(monkeypatch, tmp_path, {"claude": [str(agent_bin)]})
    package_root = tmp_path / "acpx-package"
    dist_dir = package_root / "dist"
    dist_dir.mkdir(parents=True)
    entrypoint = dist_dir / "cli.js"
    entrypoint.write_text(
        "#!/usr/bin/env node\nimport './runtime.js';\n",
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    runtime_chunk = dist_dir / "runtime.js"
    runtime_chunk.write_text("export const marker = 'v1';\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        '{"name":"acpx","version":"1.0.0","bin":{"acpx":"dist/cli.js"},"dependencies":{}}',
        encoding="utf-8",
    )
    backend = AcpxBackend(
        provider="acpx-claude-sonnet",
        model="sonnet",
        agent="claude",
        acpx_bin=str(entrypoint),
    )

    captured = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]
    runtime_chunk.write_text("export const marker = 'v2';\n", encoding="utf-8")
    changed = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]

    assert captured["profile_fingerprint"] != changed["profile_fingerprint"]


def test_acpx_transport_fingerprint_includes_transitive_dependency_code(
    monkeypatch,
    tmp_path,
) -> None:
    agent_bin = _write_acpx_entrypoint(tmp_path / "claude-agent")
    _configure_acpx_agents(monkeypatch, tmp_path, {"claude": [str(agent_bin)]})
    package_root = tmp_path / "acpx-package"
    dist_dir = package_root / "dist"
    dist_dir.mkdir(parents=True)
    entrypoint = dist_dir / "cli.js"
    entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "acpx",
                "version": "1.0.0",
                "bin": {"acpx": "dist/cli.js"},
                "dependencies": {"dep-a": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    dep_a = package_root / "node_modules" / "dep-a"
    dep_a.mkdir(parents=True)
    (dep_a / "index.js").write_text("import 'dep-b';\n", encoding="utf-8")
    (dep_a / "package.json").write_text(
        json.dumps(
            {
                "name": "dep-a",
                "version": "1.0.0",
                "dependencies": {"dep-b": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    dep_b = package_root / "node_modules" / "dep-b"
    dep_b.mkdir(parents=True)
    transitive_code = dep_b / "index.js"
    transitive_code.write_text("export const marker = 'v1';\n", encoding="utf-8")
    (dep_b / "package.json").write_text(
        json.dumps({"name": "dep-b", "version": "1.0.0", "dependencies": {}}),
        encoding="utf-8",
    )
    backend = AcpxBackend(
        provider="acpx-claude-sonnet",
        model="sonnet",
        agent="claude",
        acpx_bin=str(entrypoint),
    )

    captured = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]
    transitive_code.write_text("export const marker = 'v2';\n", encoding="utf-8")
    changed = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]

    assert captured["profile_fingerprint"] != changed["profile_fingerprint"]


def test_missing_acpx_transport_fails_closed(tmp_path) -> None:
    backend = AcpxBackend(
        provider="acpx-claude-sonnet",
        model="sonnet",
        agent="claude",
        acpx_bin=str(tmp_path / "missing-acpx"),
    )

    profile = model_profiles_from_backends([backend], repo_root=tmp_path)["acpx-claude-sonnet"]
    context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": False,
            "git_tracked_dirty": False,
        },
        model_preset="claude-sonnet",
        risk_policy=_risk_policy(),
        commission_model="ibkr",
        model_profiles={"acpx-claude-sonnet": profile},
    )

    result = decision_experiment(
        context,
        provider="acpx-claude-sonnet",
        model="sonnet",
    )

    assert profile["profile_fingerprint"] is None
    assert result["experiment_id"] is None
    assert result["decision_grade"] is False
    assert "model.profile.acpx-claude-sonnet:incomplete" in result["issues"]


def test_unverifiable_fallback_only_blocks_that_provider() -> None:
    profiles = _model_profiles()
    profiles["acpx-claude-sonnet"]["profile_fingerprint"] = None
    context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": False,
            "git_tracked_dirty": False,
        },
        model_preset="codex-luna-medium",
        risk_policy=_risk_policy(),
        commission_model="ibkr",
        model_profiles=profiles,
    )

    codex = decision_experiment(
        context,
        provider="acpx",
        model="gpt-5.6-luna",
    )
    claude = decision_experiment(
        context,
        provider="acpx-claude-sonnet",
        model="sonnet",
    )

    assert codex["decision_grade"] is True
    assert str(codex["experiment_id"]).startswith(ID_PREFIX)
    assert claude["decision_grade"] is False
    assert claude["experiment_id"] is None
    assert "model.profile.acpx-claude-sonnet:incomplete" in claude["issues"]


def test_kimi_and_grok_profiles_resolve_their_own_effective_effort(
    monkeypatch,
    tmp_path,
) -> None:
    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    (kimi_home / "config.toml").write_text(
        'default_model = "kimi-code/k3"\n[thinking]\nenabled = true\neffort = "high"\n',
        encoding="utf-8",
    )
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    (grok_home / "config.toml").write_text(
        '[models]\ndefault = "grok-4.6"\ndefault_reasoning_effort = "xhigh"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    acpx_bin = _write_acpx_entrypoint(tmp_path / "acpx")
    backends = [
        AcpxBackend(
            provider="acpx-kimi",
            model="kimi-code/k3",
            agent="kimi",
            acpx_bin=str(acpx_bin),
        ),
        AcpxBackend(
            provider="acpx-grok",
            model="grok-4.6",
            agent="grok-build",
            grok_home=str(grok_home),
            acpx_bin=str(acpx_bin),
        ),
    ]

    profiles = model_profiles_from_backends(backends, repo_root=tmp_path)

    assert profiles["acpx-kimi"]["reasoning_effort"] == "high"
    assert profiles["acpx-grok"]["reasoning_effort"] == "xhigh"


def test_openai_compatible_endpoint_splits_cohort_without_leaking_url_secrets(
    tmp_path,
) -> None:
    first_backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        model="nemotron",
        api_key="api-secret-one",
        base_url=("https://url-user:url-password@api.one.example:8443/v1/?token=query-secret-one#fragment-secret-one"),
    )
    secret_only_change = OpenAICompatibleBackend(
        provider="ollama-cloud",
        model="nemotron",
        api_key="api-secret-two",
        base_url=(
            "https://other-user:other-password@api.one.example:8443/v1?token=query-secret-two#fragment-secret-two"
        ),
    )
    endpoint_change = OpenAICompatibleBackend(
        provider="ollama-cloud",
        model="nemotron",
        api_key="api-secret-three",
        base_url="https://api.two.example:8443/v2",
    )

    first = model_profiles_from_backends([first_backend], repo_root=tmp_path)["ollama-cloud"]
    same_endpoint = model_profiles_from_backends([secret_only_change], repo_root=tmp_path)["ollama-cloud"]
    changed_endpoint = model_profiles_from_backends([endpoint_change], repo_root=tmp_path)["ollama-cloud"]

    assert first["profile_fingerprint"] == same_endpoint["profile_fingerprint"]
    assert first["profile_fingerprint"] != changed_endpoint["profile_fingerprint"]
    assert not any(
        secret in str(first)
        for secret in (
            "api-secret",
            "url-user",
            "url-password",
            "query-secret",
            "fragment-secret",
        )
    )

    def experiment_id(profile: dict) -> str | None:
        context = build_experiment_context(
            code_version={
                "git_commit": "a" * 40,
                "git_dirty": False,
                "git_tracked_dirty": False,
            },
            model_preset="ollama-nemotron",
            risk_policy=_risk_policy(),
            commission_model="ibkr",
            model_profiles={"ollama-cloud": profile},
        )
        return decision_experiment(
            context,
            provider="ollama-cloud",
            model="nemotron",
        )["experiment_id"]

    first_id = experiment_id(first)
    assert first_id is not None
    assert first_id == experiment_id(same_endpoint)
    assert first_id != experiment_id(changed_endpoint)


def test_unknown_git_cleanliness_cannot_receive_a_decision_grade_id() -> None:
    context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": None,
            "git_tracked_dirty": None,
        },
        model_preset="codex-luna-medium",
        risk_policy=_risk_policy(),
        commission_model="ibkr",
        model_profiles=_model_profiles(),
    )

    result = decision_experiment(context, provider="acpx", model="gpt-5.6-luna")

    assert result["experiment_id"] is None
    assert result["decision_grade"] is False
    assert "git_tracked_dirty:missing_or_invalid" in result["issues"]


def test_experiment_id_is_stable_and_canonical() -> None:
    first = decision_experiment(_context(), provider="acpx", model="gpt-5.6-luna")
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
        model_profiles=_model_profiles(),
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
    baseline = decision_experiment(_context(), provider="acpx", model="gpt-5.6-luna")["experiment_id"]
    fallback = decision_experiment(_context(), provider="acpx-claude-sonnet", model="sonnet")["experiment_id"]
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
        model_profiles=_model_profiles(),
    )
    risk_id = decision_experiment(risk_context, provider="acpx", model="gpt-5.6-luna")["experiment_id"]
    no_fee = decision_experiment(
        _context(commission_model="none"),
        provider="acpx",
        model="gpt-5.6-luna",
    )["experiment_id"]

    assert len({baseline, fallback, risk_id, no_fee}) == 4


def test_dirty_or_incomplete_context_fails_closed_without_reusable_id() -> None:
    dirty = decision_experiment(_context(dirty=True), provider="acpx", model="gpt-5.6-luna")
    missing_model = decision_experiment(_context(), provider="acpx", model=None)
    incomplete = deepcopy(_context())
    incomplete["risk"].pop("require_hard_stop")
    missing_risk = decision_experiment(incomplete, provider="acpx", model="gpt-5.6-luna")

    assert dirty["experiment_id"] is None
    assert "git_tracked_dirty:working_tree_not_clean" in dirty["issues"]
    assert dirty["decision_grade"] is False
    assert missing_model["experiment_id"] is None
    assert "model.model:missing" in missing_model["issues"]
    assert missing_risk["experiment_id"] is None
    assert any(issue.startswith("risk:missing_fields:") for issue in missing_risk["issues"])


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
        model_profiles=_model_profiles(),
    )

    result = decision_experiment(context, provider="acpx", model="gpt-5.6-luna")

    assert result["experiment_id"] is not None
    assert result["components"]["git_tracked_dirty"] is False
    assert "prompt_0.txt" not in str(result)


def test_inherited_experiment_revalidates_persisted_hash() -> None:
    original = decision_experiment(_context(), provider="acpx", model="gpt-5.6-luna")
    tampered = deepcopy(original)
    tampered["components"]["risk"]["max_order_value"] = 999_999.0

    assert inherited_experiment(original) == original
    assert inherited_experiment(tampered) is None


def test_model_agent_and_reasoning_effort_split_v3_cohorts() -> None:
    baseline = decision_experiment(_context(), provider="acpx", model="gpt-5.6-luna")
    high_context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": False,
            "git_tracked_dirty": False,
        },
        model_preset="codex-luna-medium",
        risk_policy=_risk_policy(),
        commission_model="ibkr",
        model_profiles=_model_profiles(reasoning_effort="high"),
    )
    kimi_context = build_experiment_context(
        code_version={
            "git_commit": "a" * 40,
            "git_dirty": False,
            "git_tracked_dirty": False,
        },
        model_preset="codex-luna-medium",
        risk_policy=_risk_policy(),
        commission_model="ibkr",
        model_profiles=_model_profiles(agent="kimi"),
    )

    high = decision_experiment(high_context, provider="acpx", model="gpt-5.6-luna")
    kimi = decision_experiment(kimi_context, provider="acpx", model="gpt-5.6-luna")

    assert len({baseline["experiment_id"], high["experiment_id"], kimi["experiment_id"]}) == 3
    assert baseline["components"]["model"]["execution_profile"]["agent"] == "codex"
    assert baseline["components"]["model"]["execution_profile"]["reasoning_effort"] == "medium"


def test_risk_schema_rejects_wrong_key_type_and_value_even_if_cardinality_matches() -> None:
    malformed = _context()
    malformed["risk"].pop("max_order_value")
    malformed["risk"]["other_limit"] = 50_000.0
    malformed["risk"]["confidence_gate_enabled"] = 0
    malformed["risk"]["min_trade_confidence"] = 1.2

    result = decision_experiment(malformed, provider="acpx", model="gpt-5.6-luna")

    assert result["experiment_id"] is None
    assert "risk:missing_fields:max_order_value" in result["issues"]
    assert "risk:unexpected_fields:other_limit" in result["issues"]
    assert "risk.confidence_gate_enabled:invalid_type" in result["issues"]
    assert "risk.min_trade_confidence:out_of_range" in result["issues"]


def test_inherited_experiment_accepts_valid_v1_but_rejects_self_hashed_bad_risk() -> None:
    current = decision_experiment(_context(), provider="acpx", model="gpt-5.6-luna")
    legacy_components = deepcopy(current["components"])
    legacy_components["model"].pop("execution_profile")
    legacy = {
        "schema_version": 1,
        "experiment_id": _experiment_id(legacy_components, prefix=LEGACY_ID_PREFIX),
        "components": legacy_components,
        "issues": [],
        "decision_grade": True,
    }
    malformed_components = deepcopy(current["components"])
    malformed_components["risk"].pop("max_order_value")
    malformed_components["risk"]["other_limit"] = 50_000.0
    malformed = {
        **current,
        "components": malformed_components,
        "experiment_id": _experiment_id(malformed_components),
    }

    assert inherited_experiment(legacy) == legacy
    assert inherited_experiment(malformed) is None


def test_inherited_experiment_accepts_valid_v2_descriptor() -> None:
    current = decision_experiment(_context(), provider="acpx", model="gpt-5.6-luna")
    previous = {
        **current,
        "schema_version": 2,
        "experiment_id": _experiment_id(
            current["components"],
            prefix=V2_ID_PREFIX,
        ),
    }

    assert inherited_experiment(previous) == previous
