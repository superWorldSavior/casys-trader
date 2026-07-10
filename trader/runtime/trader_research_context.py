"""Per-symbol research slices for trader prompts and decision trace."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from trader.domain.universe import project_company_briefs_to_universe_context
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore


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
    projected = project_company_briefs_to_universe_context(
        companies.read_current_many(selected),
        candidate_symbols=selected,
        active_at=active_at,
        mode="active",
    )
    company_by_symbol = {
        symbol: {
            "mode": mode,
            "authority": "research_context_not_trade_instruction",
            **dict(projected.symbols[symbol]),
        }
        for symbol in selected
    }
    mandate_by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in selected:
        active = mandates.active_slice_for_symbol(symbol)
        if active is not None:
            mandate_by_symbol[symbol] = {
                "mode": mode,
                "authority": "observe_only_no_deterministic_gate",
                **active,
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
    return company_by_symbol, mandate_by_symbol


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
