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

from . import attribution as attribution_mod, llm
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


_GUARDRAILS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def load_guardrails(path: Path) -> list[dict]:
    """Garde-fous invariants, écrits par l'humain (D6 : séparés des patterns machine).

    Cache par mtime : le fichier est statique en exécution normale, inutile de le
    relire à chaque cycle ; une édition humaine est prise en compte au cycle suivant.
    """
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _GUARDRAILS_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    guardrails = [entry for entry in payload if isinstance(entry, dict) and entry.get("note")]
    _GUARDRAILS_CACHE[str(path)] = (mtime, guardrails)
    return guardrails


def build_context_learnings(
    consolidated: dict,
    *,
    raw_recent: list[dict],
    guardrails: list[dict] | None = None,
) -> list[dict] | dict:
    """Contexte learnings injecté à l'agent (D6 du registre).

    `guardrails` = invariants humains, toujours présents et nommés à part —
    l'agent doit pouvoir distinguer « règle qui ne bouge pas » de « pattern
    appris, remettable en question ». Dès qu'un consolidé existe, les bruts ne
    sont plus réinjectés : ils répétaient les derniers HOLD et nourrissaient la
    boucle d'auto-renforcement.
    """
    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    if normalized is None or not _has_consolidated(normalized):
        if guardrails:
            return {"guardrails": guardrails, "raw_recent": raw_recent}
        return raw_recent
    context: dict = {
        "global": normalized["global"],
        "by_symbol": normalized["by_symbol"],
    }
    if guardrails:
        context["guardrails"] = guardrails
    return context


def build_consolidation_prompt(current: dict, new_raw: list[dict], *, attribution: dict | None = None) -> str:
    payload = {
        "current_consolidated": current,
        "new_raw_learnings": new_raw,
        "attribution": attribution,
        "schema": {
            "global": [{"note": "string", "robustness": "optional string"}],
            "by_symbol": {"SYMBOL": [{"note": "string", "robustness": "optional string"}]},
        },
        "limits": {"global": DEFAULT_MAX_GLOBAL, "by_symbol": DEFAULT_MAX_BY_SYMBOL},
    }
    return (
        "Tu es le consolidateur machine de casys-trader.\n"
        "Objectif : produire un résumé actionnable, équilibré et non redondant.\n"
        "Règles strictes :\n"
        "1. ANCRE dans les RÉSULTATS. Les notes brutes sont du contexte, pas une "
        "preuve. Mets `robustness:\"high\"` seulement si l'attribution confirme le "
        "pattern avec au moins 3 occurrences distinctes ET un P&L cohérent.\n"
        "2. ÉQUILIBRE entrée / sortie / coût. Les patterns d'ENTRÉE POSITIFS disent "
        "quand AGIR et viennent des trades GAGNANTS de l'attribution "
        "(CLOSE/take_profit/max_hold). Ajoute des patterns de GESTION DE SORTIE "
        "quand un `exit_reason` coûte dans `by_exit_reason` (lis `total_pnl` comme "
        "net et `total_commission` comme commission, ex. trailing_stop). Ajoute des "
        "patterns de COÛT si `total_commissions` ronge une part notable de "
        "`realized_gross_pnl`.\n"
        "3. QUOTA anti-abstention : AU MOINS 3 règles `global` doivent être des "
        "conditions d'ACTION positives ; AU PLUS 4 règles d'abstention dans "
        "`global`.\n"
        "4. ANTI-REDONDANCE : une règle `by_symbol` n'est gardée que si elle dit "
        "quelque chose de SPÉCIFIQUE au symbole, absent du `global`. Interdit de "
        "reformuler une règle globale par symbole.\n"
        "5. Garde les limites : fusionne les abstentions redondantes, omets ce qui "
        "répète `current_consolidated`, respecte DEFAULT_MAX_GLOBAL et "
        "DEFAULT_MAX_BY_SYMBOL, et retourne une sortie JSON pure {global, by_symbol} "
        "sans markdown.\n\n"
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
    attribution: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
) -> tuple[dict | None, dict | None]:
    router = llm_router or build_consolidator_router_from_env(
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
    )
    prompt = build_consolidation_prompt(current, new_raw, attribution=attribution)
    attempts = max(1, int(max_attempts))
    completion: llm.LlmCompletion | llm.LlmFailure | None = None
    for attempt in range(attempts):
        completion = router.complete(prompt, timeout_s=timeout_s)
        if not isinstance(completion, llm.LlmFailure):
            break
        if not completion.retryable or attempt == attempts - 1:
            return None, _failure_from_llm(completion)
    if isinstance(completion, llm.LlmFailure):
        return None, _failure_from_llm(completion)
    if completion is None:
        return None, _failure_payload("no_attempt", "no consolidation attempt was executed")
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
    attribution: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
) -> dict | None:
    consolidated, _error = _consolidate_payload_with_error(
        current,
        new_raw,
        attribution=attribution,
        llm_router=llm_router,
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
    )
    return consolidated


def maybe_consolidate(
    raw_store: LearningsStore,
    consolidated_store: ConsolidatedLearningsStore,
    *,
    threshold: int = DEFAULT_CONSOLIDATION_THRESHOLD,
    attribution: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
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
        attribution=attribution,
        llm_router=llm_router,
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
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


def _default_state_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "state"


def main(argv: list[str] | None = None) -> int:
    """CLI d'inspection et run cron : python -m trader.consolidator --run."""
    import argparse

    parser = argparse.ArgumentParser(description="Consolidateur des learnings runtime")
    parser.add_argument("--run", action="store_true", help="exécute une consolidation si le seuil est atteint")
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir(), help="répertoire state/ à utiliser")
    parser.add_argument("--threshold", type=int, default=DEFAULT_CONSOLIDATION_THRESHOLD, help="seuil de bruts nouveaux")
    parser.add_argument("--max-attempts", type=int, default=3, help="tentatives LLM intra-cycle")
    parser.add_argument("--acpx-bin", default=None, help="binaire acpx du consolidateur")
    parser.add_argument("--acpx-agent", default=None, help="agent acpx du consolidateur")
    parser.add_argument("--model", default=None, help="modèle du consolidateur")
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_CONSOLIDATOR_TIMEOUT_S, help="timeout LLM en secondes")
    parser.add_argument("--attribution-since", default=None, help="borne since déjà filtrée au régime")
    parser.add_argument(
        "--exclude-symbol",
        action="append",
        default=[],
        help="symbole à exclure de l'attribution ; répétable",
    )
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    raw_store = LearningsStore(state_dir / "learnings.jsonl", max_entries=DEFAULT_RAW_MAX_ENTRIES)
    consolidated_store = ConsolidatedLearningsStore(state_dir / "learnings_consolidated.json")

    if args.run:
        attr = attribution_mod.compute_attribution(
            state_dir,
            since=args.attribution_since,
            exclude_symbols=tuple(args.exclude_symbol),
        )
        result = maybe_consolidate(
            raw_store,
            consolidated_store,
            threshold=args.threshold,
            attribution=attr,
            acpx_bin=args.acpx_bin,
            acpx_agent=args.acpx_agent,
            model=args.model,
            timeout_s=args.timeout_s,
            max_attempts=args.max_attempts,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0

    current = consolidated_store.read()
    raw_rows = raw_store.all()
    payload = {
        "state_dir": str(state_dir),
        "raw_count": len(raw_rows),
        "new_raw_count": len(select_new_raw(raw_rows, watermark=current.get("watermark"))),
        "consolidated_watermark": current.get("watermark"),
        "has_consolidated": _has_consolidated(current),
        "last_failure": _default_status_store(consolidated_store).read().get("last_failure"),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
