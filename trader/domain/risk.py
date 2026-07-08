"""Pure risk value contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "RiskLimits",
    "Verdict",
]


def _validate_confidence_threshold(value: float, name: str) -> None:
    """Lève ValueError si value n'est pas un seuil de confiance valide."""
    if not math.isfinite(value):
        raise ValueError(f"{name}={value!r} doit être fini")
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name}={value!r} doit être dans [0, 1]")


@dataclass(frozen=True)
class RiskLimits:
    max_position_value: float       # $ max par position (valeur absolue)
    max_gross_exposure: float       # $ max exposition brute (somme |positions|)
    max_order_value: float          # $ max par ordre unique
    min_equity: float               # equity plancher : sous ce seuil, plus aucun ordre
    max_risk_per_trade_pct: float = 0.01  # % equity risqué si le hard_stop saute
    # Gate de confiance adapté au risque :
    #   required = min_trade_confidence + (full_risk_confidence − min_trade_confidence)
    #              × clamp(planned_risk_pct / max_risk_per_trade_pct, 0, 1)
    min_trade_confidence: float = 0.7   # seuil plancher (risque nul)
    full_risk_confidence: float = 0.9   # seuil exigé au budget complet (ou sans stop)
    # Paper/exploration : si False, le gate de confiance ne rejette plus AUCUN ordre
    # (la confiance devient une prédiction pure pour la calibration, découplée de la
    # taille). Défaut True = comportement live-safe inchangé. Voir spec
    # 2026-06-24-exploration-basse-confiance-calibration-design.md.
    confidence_gate_enabled: bool = True

    def __post_init__(self) -> None:
        # Fail-closed : des seuils invalides rendraient le gate inutilisable.
        _validate_confidence_threshold(self.min_trade_confidence, "min_trade_confidence")
        _validate_confidence_threshold(self.full_risk_confidence, "full_risk_confidence")
        if self.min_trade_confidence > self.full_risk_confidence:
            raise ValueError(
                f"min_trade_confidence={self.min_trade_confidence!r} "
                f"> full_risk_confidence={self.full_risk_confidence!r}"
            )

    @classmethod
    def from_dict(cls, d: dict) -> "RiskLimits":
        return cls(
            max_position_value=float(d["max_position_value"]),
            max_gross_exposure=float(d["max_gross_exposure"]),
            max_order_value=float(d["max_order_value"]),
            min_equity=float(d["min_equity"]),
            max_risk_per_trade_pct=float(d.get("max_risk_per_trade_pct", 0.01)),
            min_trade_confidence=float(d.get("min_trade_confidence", 0.7)),
            full_risk_confidence=float(d.get("full_risk_confidence", 0.9)),
            confidence_gate_enabled=bool(d.get("confidence_gate_enabled", True)),
        )


@dataclass(frozen=True)
class Verdict:
    approved: bool
    code: str = "ok"
    context: str = ""
