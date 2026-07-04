from __future__ import annotations

from datetime import datetime, timezone

from trader.application import learnings_recall
from trader.application.learnings_recall import build_recall_provider


NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class RecallStoreStub:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []

    def search(self, **kwargs) -> list[dict]:
        self.search_calls.append(kwargs)
        return [{"id": len(self.search_calls), "note": "ok"}]


def test_recall_provider_skips_embedding_without_query_and_caps_limit() -> None:
    store = RecallStoreStub()
    embed_calls: list[list[str]] = []

    def embedder(texts: list[str], **kwargs) -> list[bytes]:
        embed_calls.append(texts)
        return [b"vec"]

    provider = build_recall_provider(store, NOW, embedder=embedder)

    assert provider({"symbol": "SPY", "limit": 99}) == {"rows": [{"id": 1, "note": "ok"}]}
    assert embed_calls == []
    assert store.search_calls == [
        {
            "query_vec": None,
            "text_query": None,
            "symbol": "SPY",
            "family": None,
            "limit": 8,
            "now": NOW,
        }
    ]


def test_recall_provider_embeds_query_once_per_cycle(monkeypatch) -> None:
    store = RecallStoreStub()
    embed_calls: list[dict] = []

    def embedder(texts: list[str], **kwargs) -> list[bytes]:
        embed_calls.append({"texts": texts, "kwargs": kwargs})
        return [b"cached-vector"]

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = build_recall_provider(store, NOW, embedder=embedder)

    provider({"query": "momentum", "limit": 2})
    provider({"query": "momentum", "limit": 2})

    assert embed_calls == [{"texts": ["momentum"], "kwargs": {"api_key": "test-key"}}]
    assert [call["query_vec"] for call in store.search_calls] == [b"cached-vector", b"cached-vector"]


def test_recall_provider_default_embedder_uses_short_timeout(monkeypatch) -> None:
    store = RecallStoreStub()
    embed_calls: list[dict] = []

    def embedder(texts: list[str], **kwargs) -> list[bytes]:
        embed_calls.append({"texts": texts, "kwargs": kwargs})
        return [b"default-vector"]

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = build_recall_provider(store, NOW, default_embedder=embedder)

    provider({"query": "breakout"})

    assert embed_calls == [{"texts": ["breakout"], "kwargs": {"api_key": "test-key", "timeout_s": 3}}]


def test_recall_provider_default_embedder_is_resolved_at_build_time(monkeypatch) -> None:
    store = RecallStoreStub()
    embed_calls: list[dict] = []

    def embedder(texts: list[str], **kwargs) -> list[bytes]:
        embed_calls.append({"texts": texts, "kwargs": kwargs})
        return [b"patched-vector"]

    monkeypatch.setattr(learnings_recall.embeddings_mod, "embed_texts", embedder)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    provider = build_recall_provider(store, NOW)

    provider({"query": "breakout"})

    assert embed_calls == [{"texts": ["breakout"], "kwargs": {"api_key": "test-key", "timeout_s": 3}}]


def test_recall_provider_degrades_to_fts_when_embedding_fails(monkeypatch) -> None:
    store = RecallStoreStub()
    warnings: list[tuple] = []

    def failing_embedder(texts: list[str], **kwargs) -> list[bytes]:
        raise RuntimeError("timeout")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = build_recall_provider(
        store,
        NOW,
        embedder=failing_embedder,
        log_warning=lambda *args: warnings.append(args),
    )

    assert provider({"query": "momentum"}) == {"rows": [{"id": 1, "note": "ok"}]}
    assert store.search_calls[0]["query_vec"] is None
    assert store.search_calls[0]["text_query"] == "momentum"
    assert warnings
