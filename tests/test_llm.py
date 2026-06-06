import json

from trader.llm import (
    LlmCompletion,
    LlmFailure,
    LlmRouter,
    OpenAICompatibleBackend,
    build_default_router_from_env,
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
            model="gpt-5.3-codex-spark[medium]",
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
            model="gpt-5.3-codex-spark[medium]",
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
