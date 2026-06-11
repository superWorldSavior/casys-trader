import json
import subprocess

from trader.llm import (
    AcpxBackend,
    LlmCompletion,
    LlmFailure,
    LlmRouter,
    OpenAICompatibleBackend,
    build_acpx_command,
    build_default_router_from_env,
    _looks_retryable_provider_error,
)


class StubBackend:
    def __init__(self, result):
        self.provider = "stub"
        self.model = "stub-model"
        self.result = result
        self.calls = 0

    def complete(self, prompt: str, *, timeout_s: int):
        self.calls += 1
        return self.result


def test_router_fallback_sur_echec_retryable() -> None:
    primary = StubBackend(
        LlmFailure(
            provider="spark",
            model="gpt-5.3-codex-spark/medium",
            code="rate_limited",
            message="credit exhausted",
            retryable=True,
        )
    )
    fallback = StubBackend(
        LlmCompletion(
            provider="ollama-cloud",
            model="nemotron-3-nano:30b-cloud",
            text='{"ok":true}',
        )
    )

    result = LlmRouter([primary, fallback]).complete("prompt", timeout_s=12)

    assert isinstance(result, LlmCompletion)
    assert result.provider == "ollama-cloud"
    assert result.model == "nemotron-3-nano:30b-cloud"
    assert result.fallback_reason == "spark:rate_limited"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_router_ne_fallback_pas_sur_echec_non_retryable() -> None:
    primary = StubBackend(
        LlmFailure(
            provider="spark",
            model="gpt-5.3-codex-spark/medium",
            code="bad_output",
            message="invalid JSON",
            retryable=False,
        )
    )
    fallback = StubBackend(
        LlmCompletion(
            provider="ollama-cloud",
            model="nemotron-3-nano:30b-cloud",
            text='{"ok":true}',
        )
    )

    result = LlmRouter([primary, fallback]).complete("prompt", timeout_s=12)

    assert isinstance(result, LlmFailure)
    assert result.code == "bad_output"
    assert fallback.calls == 0


def test_router_ne_fallback_pas_sur_timeout_non_retryable() -> None:
    primary = StubBackend(
        LlmFailure(
            provider="spark",
            model="gpt-5.3-codex-spark/medium",
            code="timeout",
            message="> 900s",
            retryable=False,
        )
    )
    fallback = StubBackend(
        LlmCompletion(
            provider="ollama-cloud",
            model="nemotron-3-nano:30b-cloud",
            text='{"ok":true}',
        )
    )

    result = LlmRouter([primary, fallback]).complete("prompt", timeout_s=900)

    assert isinstance(result, LlmFailure)
    assert result.code == "timeout"
    assert fallback.calls == 0


def test_retryable_provider_error_se_limite_aux_rate_limits_et_quotas() -> None:
    assert _looks_retryable_provider_error("HTTP 429")
    assert _looks_retryable_provider_error("rate limit exceeded")
    assert _looks_retryable_provider_error("insufficient_quota")
    assert _looks_retryable_provider_error("quota exhausted")
    assert not _looks_retryable_provider_error("timeout")
    assert not _looks_retryable_provider_error("timed out")
    assert not _looks_retryable_provider_error("overloaded")
    assert not _looks_retryable_provider_error("erreur fournisseur generique")


def test_acpx_backend_timeout_est_un_echec_non_retryable(monkeypatch) -> None:
    monkeypatch.setattr("trader.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.llm._terminate_process_group", lambda _pid: None)

    class TimeoutPopen:
        pid = 4242
        returncode = None

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout)

        def kill(self):
            return None

    monkeypatch.setattr("trader.llm.subprocess.Popen", TimeoutPopen)

    result = AcpxBackend().complete("prompt", timeout_s=900)

    assert isinstance(result, LlmFailure)
    assert result.code == "timeout"
    assert result.retryable is False


def test_acpx_backend_isole_et_nettoie_le_process_group(monkeypatch) -> None:
    monkeypatch.setattr("trader.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    popen_calls = []
    cleaned_pids = []

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.llm.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.llm._terminate_process_group", lambda pid: cleaned_pids.append(pid))

    result = AcpxBackend().complete("prompt", timeout_s=12)

    assert isinstance(result, LlmCompletion)
    assert result.text == "OK"
    assert popen_calls[0][1]["stdout"] is subprocess.PIPE
    assert popen_calls[0][1]["stderr"] is subprocess.PIPE
    assert popen_calls[0][1]["text"] is True
    assert popen_calls[0][1]["start_new_session"] is True
    assert cleaned_pids == [4242]


def test_openai_compatible_backend_appelle_chat_completions() -> None:
    calls = []

    def post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
        calls.append((url, payload, headers, timeout_s))
        return {"choices": [{"message": {"content": '{"symbol":"SPY","action":"HOLD"}'}}]}

    backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        api_key="secret",
        base_url="https://ollama.com/v1",
        model="nemotron-3-nano:30b-cloud",
        post_json=post_json,
    )

    result = backend.complete("Décide en JSON", timeout_s=42)

    assert isinstance(result, LlmCompletion)
    assert result.text == '{"symbol":"SPY","action":"HOLD"}'
    assert result.provider == "ollama-cloud"
    assert calls == [
        (
            "https://ollama.com/v1/chat/completions",
            {
                "model": "nemotron-3-nano:30b-cloud",
                "messages": [{"role": "user", "content": "Décide en JSON"}],
                "temperature": 0,
            },
            {"Authorization": "Bearer secret", "Content-Type": "application/json"},
            42,
        )
    ]


def test_openai_compatible_backend_classe_429_retryable() -> None:
    def post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
        raise RuntimeError(json.dumps({"status": 429, "body": "rate limit"}))

    backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        api_key="secret",
        base_url="https://ollama.com/v1",
        model="nemotron-3-nano:30b-cloud",
        post_json=post_json,
    )

    result = backend.complete("prompt", timeout_s=12)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "rate_limited"


def test_build_acpx_command_peut_cibler_un_agent_dedie() -> None:
    cmd = build_acpx_command(
        "consolide ces learnings",
        acpx_bin="acpx",
        model="gpt-5.5/high",
        timeout_s=240,
        agent="codex",
    )

    assert cmd == [
        "acpx",
        "--format",
        "quiet",
        "--allowed-tools",
        "",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "--model",
        "gpt-5.5/high",
        "--timeout",
        "240",
        "codex",
        "exec",
        "consolide ces learnings",
    ]


def test_build_acpx_command_peut_prefixer_un_label_identifiable() -> None:
    cmd = build_acpx_command(
        "prompt",
        acpx_bin="acpx",
        model="gpt-5.3-codex-spark/medium",
        timeout_s=12,
        session_label="casys-trader:runtime-brain",
    )

    assert cmd[-1].startswith("[casys-trader:runtime-brain]\n")
    assert cmd[-1].endswith("prompt")


def test_build_default_router_from_env_peut_nommer_lagent_acpx(monkeypatch) -> None:
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    router = build_default_router_from_env(
        env_path=None,
        acpx_bin="acpx-custom",
        spark_model="gpt-5.5/high",
        acpx_provider="consolidator",
        acpx_agent="codex",
    )

    backend = router.backends[0]
    assert isinstance(backend, AcpxBackend)
    assert backend.provider == "consolidator"
    assert backend.model == "gpt-5.5/high"
    assert backend.acpx_bin == "acpx-custom"
    assert backend.agent == "codex"
    assert backend.session_label == "casys-trader:learning-consolidator"


def test_build_default_router_from_env_labelle_le_brain_runtime(monkeypatch) -> None:
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    router = build_default_router_from_env(env_path=None)

    backend = router.backends[0]
    assert isinstance(backend, AcpxBackend)
    assert backend.session_label == "casys-trader:runtime-brain"


def test_build_default_router_from_env_configure_spark_puis_ollama(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_OLLAMA_API_KEY", "secret")
    monkeypatch.setenv("TRADER_OLLAMA_MODEL", "nemotron-3-nano:30b-cloud")

    router = build_default_router_from_env()

    assert [backend.provider for backend in router.backends] == ["spark", "ollama-cloud"]
    assert router.backends[1].model == "nemotron-3-nano:30b-cloud"


def test_build_default_router_from_env_charge_un_dotenv_local(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_OLLAMA_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TRADER_OLLAMA_API_KEY=secret-from-file\n"
        "TRADER_OLLAMA_BASE_URL=https://ollama.com/v1\n"
        "TRADER_OLLAMA_MODEL=nemotron-3-nano:30b-cloud\n"
    )

    router = build_default_router_from_env(env_path=env_path)

    assert [backend.provider for backend in router.backends] == ["spark", "ollama-cloud"]
    assert router.backends[1].model == "nemotron-3-nano:30b-cloud"
