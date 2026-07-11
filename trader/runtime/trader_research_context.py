"""Per-symbol research slices for trader prompts and decision trace."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trader.domain.universe import project_company_briefs_to_universe_context
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.runtime.company_context_config import load_company_context_projection_limits


def load_trader_research_context(
    *,
    config_dir: str | Path,
    state_dir: str | Path,
    symbols: Iterable[str],
    active_at: datetime | str,
    held_symbols: Iterable[str] = (),
    company_store: CompanyIntelligenceStore | None = None,
    mandate_store: UniverseMandateStore | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load once per cycle and never expose another company's slice."""

    selected = tuple(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    held = {str(symbol).strip() for symbol in held_symbols if str(symbol).strip()}
    state_path = Path(state_dir)
    companies = company_store or CompanyIntelligenceStore(state_path / "company_intelligence")
    mandates = mandate_store or UniverseMandateStore(state_path / "universe_mandates")
    mode = _trader_context_mode(Path(config_dir))
    limits = load_company_context_projection_limits(config_dir)
    projected = project_company_briefs_to_universe_context(
        companies.read_current_many(selected),
        candidate_symbols=selected,
        active_at=active_at,
        mode="active",
        limits=limits,
    )
    company_by_symbol: dict[str, dict[str, Any]] = {}
    mandate_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in selected:
        current_company = dict(projected.symbols[symbol])
        active = mandates.active_slice_for_symbol(symbol)
        if active is not None:
            symbol_mandate = active.get("symbol_mandate")
            mandate_by_symbol[symbol] = {
                "mode": mode,
                "authority": "observe_only_no_deterministic_gate",
                **active,
            }
            mandate_company = (
                symbol_mandate.get("company_context")
                if isinstance(symbol_mandate, Mapping)
                else None
            )
            if _requires_company_delta(current_company, mandate_company):
                company_by_symbol[symbol] = {
                    "mode": mode,
                    "authority": "newer_than_universe_mandate",
                    "delta_from_mandate_as_of": (
                        str(mandate_company.get("as_of") or "")
                        if isinstance(mandate_company, Mapping)
                        else None
                    ),
                    **_company_delta(current_company),
                }
        elif symbol in held:
            mandate_by_symbol[symbol] = {
                "mode": mode,
                "authority": "observe_only_no_deterministic_gate",
                "mandate_ref": None,
                "symbol_mandate": {
                    "symbol": symbol,
                    "why_selected": "",
                    "role": "managed_existing",
                    "posture": "sticky_unmandated",
                    "allowed_sides": [],
                    "reduce_close_always_allowed": True,
                },
            }
            company_by_symbol[symbol] = {
                "mode": mode,
                "authority": "research_context_not_trade_instruction",
                **_company_delta(current_company),
            }
        else:
            company_by_symbol[symbol] = {
                "mode": mode,
                "authority": "research_context_not_trade_instruction",
                **_company_delta(current_company),
            }
    return company_by_symbol, mandate_by_symbol


def _requires_company_delta(
    current_company: Mapping[str, Any],
    mandate_company: object,
) -> bool:
    """Push only a newer evidence snapshot, while upgrading unversioned legacy mandates."""
    if not isinstance(mandate_company, Mapping) or not mandate_company:
        return True
    current_as_of = _parse_as_of(current_company.get("as_of"))
    mandate_as_of = _parse_as_of(mandate_company.get("as_of"))
    if current_as_of is None:
        return False
    if mandate_as_of is None:
        return True
    current_signature = str(current_company.get("input_signature") or "").strip()
    mandate_signature = str(mandate_company.get("input_signature") or "").strip()
    if current_signature and mandate_signature and current_signature == mandate_signature:
        return False
    return current_as_of > mandate_as_of


def _parse_as_of(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _company_delta(current_company: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the trader-side fresh micro update bounded and sourceable."""
    keys = (
        "status",
        "brief_ref",
        "as_of",
        "input_signature",
        "freshness",
        "selection_view",
        "summary",
        "drivers",
        "catalysts",
        "risks",
        "source_refs",
    )
    return {key: current_company[key] for key in keys if key in current_company}


def _trader_context_mode(config_dir: Path) -> str:
    try:
        payload = yaml.safe_load((config_dir / "company_intelligence.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        payload = {}
    if not isinstance(payload, Mapping):
        return "observe"
    mode = str(payload.get("trader_company_context_mode") or "observe").strip().lower()
    return mode if mode in {"observe", "active"} else "observe"


__all__ = ["load_trader_research_context"]
