"""Application provider for recall_learnings domain-tool lookups."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from trader.agent.learnings import embeddings as embeddings_mod

log = logging.getLogger(__name__)


class RecallSearchStore(Protocol):
    def search(
        self,
        *,
        query_vec: bytes | None,
        text_query: object,
        symbol: object,
        family: object,
        limit: int,
        now: datetime,
    ) -> list[dict]: ...


class TextEmbedder(Protocol):
    def __call__(self, texts: list[str], **kwargs: object) -> list[bytes]: ...


WarningLogger = Callable[..., None]
EnvGetter = Callable[[str], str | None]


def build_recall_provider(
    store: RecallSearchStore,
    now: datetime,
    *,
    embedder: TextEmbedder | None = None,
    default_embedder: TextEmbedder | None = None,
    env_get: EnvGetter = os.getenv,
    log_warning: WarningLogger | None = None,
) -> Callable[[dict], dict]:
    """Build a run-local recall provider with embedding cache and FTS fallback."""
    use_default_embedder = embedder is None
    if use_default_embedder:
        selected_embedder = default_embedder or embeddings_mod.embed_texts
    else:
        selected_embedder = embedder
    warning = log.warning if log_warning is None else log_warning
    embed_cache: dict[str, bytes] = {}

    def provider(args: dict) -> dict:
        query = args.get("query")
        query_vec: bytes | None = None
        if query:
            query_key = str(query)
            if query_key in embed_cache:
                query_vec = embed_cache[query_key]
            else:
                api_key = env_get("OPENAI_API_KEY")
                if api_key:
                    try:
                        if use_default_embedder:
                            blobs = selected_embedder([query_key], api_key=api_key, timeout_s=3)
                        else:
                            blobs = selected_embedder([query_key], api_key=api_key)
                        query_vec = blobs[0] if blobs else None
                    except Exception:  # noqa: BLE001 - optional embedding, FTS fallback
                        warning("learnings_recall: embed échoué, dégradation FTS5")
                        query_vec = None
                    if query_vec is not None:
                        embed_cache[query_key] = query_vec

        limit = min(int(args.get("limit") or 5), 8)
        rows = store.search(
            query_vec=query_vec,
            text_query=query,
            symbol=args.get("symbol"),
            family=args.get("family"),
            limit=limit,
            now=now,
        )
        return {"rows": rows}

    return provider
