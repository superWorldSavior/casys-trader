"""Small injectable JSON HTTP client for company-source adapters."""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def fetch_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: int = 20,
) -> dict[str, Any]:
    query = urlencode([(key, value) for key, value in (params or {}).items() if value is not None])
    target = f"{url}?{query}" if query else url
    request = Request(target, headers=dict(headers or {}))
    with urlopen(request, timeout=max(1, int(timeout_s))) as response:  # noqa: S310 - fixed provider URLs
        body = response.read()
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        if content_encoding == "gzip":
            body = gzip.decompress(body)
        elif content_encoding == "deflate":
            body = zlib.decompress(body)
        payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("provider response must be a JSON object")
    return payload


__all__ = ["fetch_json"]
