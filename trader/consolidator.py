"""Consolidateur machine-owned des learnings runtime.

Le store brut `learnings.jsonl` reste un buffer jetable. Ce module synthétise
périodiquement ces notes vers `learnings_consolidated.json`, séparé de la mémoire
humaine `mandate/memory.md`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import llm
from .tools.memory import LearningsStore

DEFAULT_RAW_MAX_ENTRIES = 200
DEFAULT_CONSOLIDATION_THRESHOLD = 50
DEFAULT_MAX_GLOBAL = 10
DEFAULT_MAX_BY_SYMBOL = 5
DEFAULT_CONSOLIDATOR_PROVIDER = "consolidator"
DEFAULT_CONSOLIDATOR_ACPX_AGENT = "codex"
DEFAULT_CONSOLIDATOR_MODEL = "gpt-5.5/high"
DEFAULT_CONSOLIDATOR_TIMEOUT_S = 240


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def empty_consolidated() -> dict:
    return {"watermark": None, "global": [], "by_symbol": {}}


def select_new_raw(raw_rows: list[dict], watermark: str | None) -> list[dict]:
    watermark_dt = _parse_ts(watermark)
    selected: list[dict] = []
    for row in raw_rows:
        row_ts = _parse_ts(row.get("ts"))
        if row_ts is None:
            continue
        if watermark_dt is None or row_ts > watermark_dt:
            selected.append(row)
    selected.sort(key=lambda item: _parse_ts(item.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return selected


def _max_ts(rows: list[dict]) -> str | None:
    parsed = [_parse_ts(row.get("ts")) for row in rows]
    valid = [item for item in parsed if item is not None]
    if not valid:
        return None
    return max(valid).isoformat()


def _normalize_entry(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    note = str(item.get("note") or "").strip()
    if not note:
        return None
    entry = {"note": note}
    robustness = str(item.get("robustness") or "").strip()
    if robustness:
        entry["robustness"] = robustness
    return entry


def _normalize_entries(items: Any, *, limit: int) -> list[dict]:
    if not isinstance(items, list):
        return []
    entries: list[dict] = []
    for item in items:
        entry = _normalize_entry(item)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def normalize_consolidated(payload: Any, *, watermark: str | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    by_symbol_raw = payload.get("by_symbol", {})
    if not isinstance(by_symbol_raw, dict):
        return None

    by_symbol: dict[str, list[dict]] = {}
    for raw_symbol, items in by_symbol_raw.items():
        symbol = str(raw_symbol).strip()
        if not symbol:
            continue
        entries = _normalize_entries(items, limit=DEFAULT_MAX_BY_SYMBOL)
        if entries:
            by_symbol[symbol] = entries

    return {
        "watermark": watermark,
        "global": _normalize_entries(payload.get("global", []), limit=DEFAULT_MAX_GLOBAL),
        "by_symbol": by_symbol,
    }


class ConsolidatedLearningsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict:
        if not self.path.exists():
            return empty_consolidated()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return empty_consolidated()
        normalized = normalize_consolidated(payload, watermark=payload.get("watermark"))
        return normalized or empty_consolidated()

    def write(self, payload: dict, *, watermark: str | None) -> None:
        normalized = normalize_consolidated(payload, watermark=watermark)
        if normalized is None:
            raise ValueError("invalid consolidated learnings payload")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class ConsolidationStatusStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_failure(
        self,
        *,
        consolidated_watermark: str | None,
        raw_watermark: str | None,
        new_raw_count: int,
        error: dict,
    ) -> None:
        payload = {
            "last_failure": {
                "consolidated_watermark": consolidated_watermark,
                "raw_watermark": raw_watermark,
                "new_raw_count": new_raw_count,
                "error_code": str(error.get("error_code") or "unknown"),
                "error_message": str(error.get("error_message") or "")[:500],
                "provider": error.get("provider"),
                "model": error.get("model"),
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


def _has_consolidated(payload: dict) -> bool:
    return bool(payload.get("global") or payload.get("by_symbol"))


def build_context_learnings(
    consolidated: dict,
    *,
    raw_recent: list[dict],
    max_raw_recent: int = 3,
) -> list[dict] | dict:
    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    if normalized is None or not _has_consolidated(normalized):
        return raw_recent
    return {
        "global": normalized["global"],
        "by_symbol": normalized["by_symbol"],
        "raw_recent": raw_recent[-max(0, max_raw_recent) :],
    }


def build_consolidation_prompt(current: dict, new_raw: list[dict]) -> str:
    payload = {
        "current_consolidated": current,
        "new_raw_learnings": new_raw,
        "schema": {
            "global": [{"note": "string", "robustness": "optional string"}],
            "by_symbol": {"SYMBOL": [{"note": "string", "robustness": "optional string"}]},
        },
        "limits": {"global": DEFAULT_MAX_GLOBAL, "by_symbol": DEFAULT_MAX_BY_SYMBOL},
    }
    return (
        "Tu es le consolidateur machine de casys-trader.\n"
        "Fusionne les learnings bruts redondants, promeus au global ce qui est transversal, "
        "garde par symbole ce qui est spécifique, et préserve les entrées stables existantes.\n"
        "Retourne uniquement un objet JSON avec les clés global et by_symbol. "
        "N'ajoute pas de markdown.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _resolved_consolidator_model(model: str | None) -> str:
    return _clean_optional(model) or os.environ.get("TRADER_CONSOLIDATOR_MODEL") or DEFAULT_CONSOLIDATOR_MODEL


def build_consolidator_router_from_env(
    *,
    env_path: str | Path | None = llm.DEFAULT_ENV_PATH,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
) -> llm.LlmRouter:
    llm.load_dotenv(env_path)
    resolved_bin = _clean_optional(acpx_bin) or os.environ.get("TRADER_CONSOLIDATOR_ACPX_BIN") or "acpx"
    resolved_agent = (
        _clean_optional(acpx_agent)
        or os.environ.get("TRADER_CONSOLIDATOR_ACPX_AGENT")
        or DEFAULT_CONSOLIDATOR_ACPX_AGENT
    )
    resolved_model = _resolved_consolidator_model(model)
    return llm.build_default_router_from_env(
        env_path=None,
        acpx_bin=resolved_bin,
        spark_model=resolved_model,
        acpx_provider=DEFAULT_CONSOLIDATOR_PROVIDER,
        acpx_agent=resolved_agent,
    )


def _default_status_store(consolidated_store: ConsolidatedLearningsStore) -> ConsolidationStatusStore:
    return ConsolidationStatusStore(consolidated_store.path.with_name("learnings_consolidation_status.json"))


def _failure_from_llm(completion: llm.LlmFailure) -> dict:
    return {
        "error_code": completion.code,
        "error_message": completion.message[:500],
        "provider": completion.provider,
        "model": completion.model,
    }


def _failure_payload(code: str, message: str) -> dict:
    return {"error_code": code, "error_message": message[:500]}


def _consolidate_payload_with_error(
    current: dict,
    new_raw: list[dict],
    *,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
) -> tuple[dict | None, dict | None]:
    router = llm_router or build_consolidator_router_from_env(
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
    )
    completion = router.complete(build_consolidation_prompt(current, new_raw), timeout_s=timeout_s)
    if isinstance(completion, llm.LlmFailure):
        return None, _failure_from_llm(completion)
    try:
        payload = json.loads(completion.text)
    except json.JSONDecodeError as exc:
        return None, _failure_payload("invalid_json", str(exc))
    normalized = normalize_consolidated(payload, watermark=current.get("watermark"))
    if normalized is None:
        return None, _failure_payload("invalid_payload", "consolidateur returned invalid payload")
    return normalized, None


def consolidate_payload(
    current: dict,
    new_raw: list[dict],
    *,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
) -> dict | None:
    consolidated, _error = _consolidate_payload_with_error(
        current,
        new_raw,
        llm_router=llm_router,
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
        timeout_s=timeout_s,
    )
    return consolidated


def maybe_consolidate(
    raw_store: LearningsStore,
    consolidated_store: ConsolidatedLearningsStore,
    *,
    threshold: int = DEFAULT_CONSOLIDATION_THRESHOLD,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    status_store: ConsolidationStatusStore | None = None,
) -> dict:
    current = consolidated_store.read()
    raw_rows = raw_store.all()
    new_raw = select_new_raw(raw_rows, watermark=current.get("watermark"))
    if len(new_raw) < max(1, threshold):
        return {"triggered": False, "new_raw_count": len(new_raw)}

    status_store = status_store or _default_status_store(consolidated_store)
    status = status_store.read()
    last_failure = status.get("last_failure")
    requested_model = _resolved_consolidator_model(model)
    if (
        isinstance(last_failure, dict)
        and last_failure.get("consolidated_watermark") == current.get("watermark")
        and last_failure.get("model") in (None, requested_model)
    ):
        raw_since_failure = select_new_raw(raw_rows, watermark=last_failure.get("raw_watermark"))
        retry_after_new_raw = max(1, threshold)
        if len(raw_since_failure) < retry_after_new_raw:
            return {
                "triggered": False,
                "new_raw_count": len(new_raw),
                "skipped": True,
                "reason": "previous_failure_backoff",
                "new_raw_since_failure": len(raw_since_failure),
                "retry_after_new_raw": retry_after_new_raw,
                "last_error_code": last_failure.get("error_code"),
            }

    consolidated, error = _consolidate_payload_with_error(
        current,
        new_raw,
        llm_router=llm_router,
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
        timeout_s=timeout_s,
    )
    if consolidated is None:
        error = error or _failure_payload("unknown", "unknown consolidation failure")
        status_store.write_failure(
            consolidated_watermark=current.get("watermark"),
            raw_watermark=_max_ts(new_raw),
            new_raw_count=len(new_raw),
            error=error,
        )
        return {
            "triggered": True,
            "new_raw_count": len(new_raw),
            "written": False,
            "error_code": error["error_code"],
            "error_message": error["error_message"],
        }

    watermark = _max_ts(new_raw)
    consolidated_store.write(consolidated, watermark=watermark)
    status_store.clear()
    return {"triggered": True, "new_raw_count": len(new_raw), "written": True}
