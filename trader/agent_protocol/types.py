"""Protocol contracts returned by the agent transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Action = Literal["BUY", "SELL", "HOLD"]
Intent = Literal["OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "HOLD", "ADD"]


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: Action
    quantity: float  # nombre d'unités ; ignoré si HOLD
    confidence: float  # 0..1
    rationale: str
    next_wake_in_minutes: float | None = None  # override du timer pour CE symbole (relatif)
    next_wake_event: str | None = None  # réveil calendaire : "session_open" | "pre_earnings" | "macro_event"
    intent: Intent | None = None
    exit_plan: dict[str, Any] | None = None
    indicator_watch: dict[str, Any] | None = None
    cancel_watch_ids: list[str] = field(default_factory=list)  # plans/veilles à annuler (correction)
    context_request: dict | None = None
    learning: str | None = None  # note runtime que l'agent veut retenir (boucle de feedback)
    thesis: dict | None = None  # L6 tag structuré {setup, horizon, invalidation} — persisté pour attribution RAG
    domain_tools: dict | None = None  # traces tournée d'outils (runtime.tool_*)
    amend_exit: dict | None = None  # L3 — patch plan de sortie ouvert (hard_stop?, take_profits?, trailing_stop?, profit_protection?)
    decision_reason_code: str = "UNKNOWN"
    resolve_from_position: bool = False   # CLOSE/REDUCE/REVERSE sans side : dériver depuis la position
    position_resolved: bool = False  # True une fois action/qty dérivées depuis la position
    reduce_fraction: float | None = None  # REDUCE : fraction de la position à réduire (0.5 = moitié)
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None
    llm_error: str | None = None
    # L1 — sizing en risque : % d'equity à risquer sur ce trade.
    # Présent = qty=0.0 est un placeholder ; le daemon dérive la qty depuis
    # risk_pct_target * equity / (stop_distance * fx_rate).
    risk_pct_target: float | None = None

    @staticmethod
    def hold(symbol: str, reason: str) -> "Decision":
        return Decision(symbol=symbol, action="HOLD", quantity=0.0, confidence=0.0, rationale=reason, intent="HOLD")


@dataclass(frozen=True)
class IndicatorRequest:
    symbol: str
    indicators: list[str]
    window: int = 48
    timeframe: str = "1h"
    lookback: str | None = None
    as_of: str = "latest"


@dataclass(frozen=True)
class ContextResearchRequest:
    symbol: str
    rationale: str
    requests: list[IndicatorRequest]
    next_wake_in_minutes: float | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None


@dataclass(frozen=True)
class BatchToolCallRequest:
    """Le lot a répondu par une tournée d'outils au lieu de décisions finales.

    `calls` reste BRUT (list[dict]) : la validation vit dans trader.agent_tools,
    côté daemon — codex_client reste un transport sans dépendance domaine."""

    calls: list[dict]
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None
