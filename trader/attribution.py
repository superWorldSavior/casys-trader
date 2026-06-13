"""attribution — relie chaque trade clôturé à sa décision d'entrée.

Module PUR en lecture seule (comme `stats`) : reconstruit les round-trips depuis
`state/model_performance.jsonl` (un fill exécuté par ligne) et en dérive le signal
qui dit si l'agent décide bien — P&L réalisé par trade, calibration de la
confidence, et répartition par raison de sortie.

N'importe jamais daemon (pas de cycle). Déterministe : mêmes logs -> même sortie.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

# En-dessous, une position est considérée comme plate (évite les poussières
# flottantes après des clôtures décimales exactes qui créeraient de faux micro-trades).
_FLAT_EPS = 1e-9

# Buckets de confidence pour la calibration (borne basse incluse, haute exclue
# sauf le dernier). Permet de répondre : « les calls confiants gagnent-ils ? »
_CONFIDENCE_BUCKETS = [
    ("0.0-0.5", 0.0, 0.5),
    ("0.5-0.7", 0.5, 0.7),
    ("0.7-0.85", 0.7, 0.85),
    ("0.85-1.0", 0.85, 1.0001),
]


def _read_perf_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "model_performance.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):  # ignore les JSON valides mais non-objets
            rows.append(obj)
    return rows


def _holding_minutes(entry_ts: str, exit_ts: str) -> float | None:
    try:
        entry = datetime.fromisoformat(entry_ts)
        exit_ = datetime.fromisoformat(exit_ts)
    except ValueError:
        return None
    return (exit_ - entry).total_seconds() / 60.0


class _OpenLeg:
    """Position ouverte d'un symbole : quantité signée, coût moyen, métadonnées d'entrée."""

    def __init__(self) -> None:
        self.qty = 0.0           # signé : >0 long, <0 short
        self.avg_price = 0.0
        self.commission = 0.0
        self.entry_ts: str | None = None
        self._conf_sum = 0.0     # somme pondérée par quantité ajoutée
        self._conf_qty = 0.0

    @property
    def entry_confidence(self) -> float | None:
        return self._conf_sum / self._conf_qty if self._conf_qty else None

    def open_or_add(
        self,
        *,
        added_qty: float,
        price: float,
        ts: str,
        confidence: float | None,
        commission: float = 0.0,
    ) -> None:
        if self.qty == 0.0:
            self.entry_ts = ts
            self._conf_sum = 0.0
            self._conf_qty = 0.0
            self.commission = 0.0
        new_qty = self.qty + added_qty
        self.avg_price = (self.avg_price * abs(self.qty) + price * abs(added_qty)) / abs(new_qty)
        self.qty = new_qty
        self.commission += max(commission, 0.0)
        if confidence is not None:
            self._conf_sum += confidence * abs(added_qty)
            self._conf_qty += abs(added_qty)

    def scale_entry_weight(self, factor: float) -> None:
        """Après une réduction partielle : réduit le poids des accumulateurs de
        confidence proportionnellement à la quantité restante (préserve la moyenne,
        évite de surpondérer la quantité déjà clôturée lors d'un futur ajout)."""
        self._conf_sum *= factor
        self._conf_qty *= factor


def _as_non_negative_float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed) or parsed < 0.0:
        return 0.0
    return parsed


def compute_round_trips(state_dir: Path) -> list[dict]:
    """Reconstruit les trades clôturés (round-trips) depuis model_performance.jsonl.

    Rejoue les fills par symbole dans l'ordre temporel, suit le coût moyen, et
    émet un round-trip à chaque réduction/clôture (gère partiel et reversal).
    """
    rows = _read_perf_rows(state_dir)
    rows.sort(key=lambda r: (str(r.get("symbol")), str(r.get("ts"))))

    legs: dict[str, _OpenLeg] = {}
    trips: list[dict] = []

    for row in rows:
        symbol = str(row.get("symbol"))
        action = str(row.get("action", "")).upper()
        if action not in ("BUY", "SELL") or row.get("symbol") is None:
            continue
        try:
            qty = abs(float(row["quantity"]))
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        # Rejette les lignes corrompues : prix/quantité manquants, nuls ou non finis.
        if not (math.isfinite(qty) and math.isfinite(price)) or qty <= 0.0 or price <= 0.0:
            continue
        ts = str(row.get("ts"))
        confidence = row.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else None
        row_commission = _as_non_negative_float(row.get("commission"))
        signed = qty if action == "BUY" else -qty

        leg = legs.setdefault(symbol, _OpenLeg())

        # Ouverture ou renforcement (même sens, ou depuis flat).
        if abs(leg.qty) <= _FLAT_EPS or (leg.qty > 0) == (signed > 0):
            leg.open_or_add(
                added_qty=signed,
                price=price,
                ts=ts,
                confidence=confidence,
                commission=row_commission,
            )
            continue

        # Sens opposé : on clôture tout ou partie de la jambe ouverte.
        closing_qty = min(qty, abs(leg.qty))
        entry_sign = 1.0 if leg.qty > 0 else -1.0
        old_abs = abs(leg.qty)
        entry_commission = leg.commission * (closing_qty / old_abs) if old_abs > 0 else 0.0
        exit_commission = row_commission * (closing_qty / qty) if qty > 0 else 0.0
        gross_pnl = (price - leg.avg_price) * closing_qty * entry_sign
        total_commission = entry_commission + exit_commission
        trips.append(
            {
                "symbol": symbol,
                "side": "LONG" if leg.qty > 0 else "SHORT",
                "quantity": closing_qty,
                "entry_price": leg.avg_price,
                "exit_price": price,
                "gross_pnl": gross_pnl,
                "commission": total_commission,
                "pnl": gross_pnl - total_commission,
                "entry_ts": leg.entry_ts,
                "exit_ts": ts,
                "holding_minutes": _holding_minutes(leg.entry_ts or ts, ts),
                "entry_confidence": leg.entry_confidence,
                "exit_reason": row.get("exit_reason") or str(row.get("intent") or ""),
            }
        )

        remaining = qty - closing_qty
        leg.qty += signed
        if abs(leg.qty) <= _FLAT_EPS:
            legs[symbol] = _OpenLeg()
        elif remaining > _FLAT_EPS:
            # Reversal : nouvelle jambe dans le sens opposé avec le reliquat.
            fresh = _OpenLeg()
            fresh.open_or_add(
                added_qty=entry_sign * -remaining,
                price=price,
                ts=ts,
                confidence=confidence,
                commission=max(row_commission - exit_commission, 0.0),
            )
            legs[symbol] = fresh
        else:
            # Réduction partielle (même sens) : on garde coût moyen, ts et confidence
            # d'entrée, mais on réduit le poids de la confidence à la quantité restante.
            leg.commission = max(leg.commission - entry_commission, 0.0)
            leg.scale_entry_weight(abs(leg.qty) / old_abs)

    return trips


def _bucket_for(confidence: float | None) -> str | None:
    if confidence is None:
        return None
    for name, low, high in _CONFIDENCE_BUCKETS:
        if low <= confidence < high:
            return name
    return None


def _aggregate(trips: list[dict]) -> dict:
    pnls = [t["pnl"] for t in trips]
    wins = [p for p in pnls if p > 0]
    return {
        "n": len(trips),
        "total_pnl": round(sum(pnls), 4),
        "total_gross_pnl": round(sum(t["gross_pnl"] for t in trips), 4),
        "total_commission": round(sum(t["commission"] for t in trips), 4),
        "win_rate": (len(wins) / len(trips)) if trips else None,
        "avg_pnl": (sum(pnls) / len(trips)) if trips else None,
    }


def compute_attribution(state_dir: Path) -> dict:
    """Round-trips + calibration par bucket de confidence + breakdown raison de sortie.

    Sortie machine-readable compacte, injectable dans le contexte de l'agent pour
    qu'il s'auto-corrige (ses calls confiants gagnent-ils ? quelles sorties coûtent ?).
    """
    trips = compute_round_trips(state_dir)
    overall = _aggregate(trips)

    by_confidence: list[dict] = []
    for name, _, _ in _CONFIDENCE_BUCKETS:
        bucket_trips = [t for t in trips if _bucket_for(t["entry_confidence"]) == name]
        if bucket_trips:
            by_confidence.append({"bucket": name, **_aggregate(bucket_trips)})

    by_exit_reason: list[dict] = []
    reasons = sorted({t["exit_reason"] for t in trips if t["exit_reason"]})
    for reason in reasons:
        reason_trips = [t for t in trips if t["exit_reason"] == reason]
        by_exit_reason.append({"reason": reason, **_aggregate(reason_trips)})

    holding = [t["holding_minutes"] for t in trips if t["holding_minutes"] is not None]
    return {
        "n_closed_trades": overall["n"],
        "realized_pnl": overall["total_pnl"],
        # Brut et frais séparés : un P&L brut ~nul avec des commissions positives
        # signale un sur-trading (scalps neutres rendus perdants par les frais).
        "realized_gross_pnl": overall["total_gross_pnl"],
        "total_commissions": overall["total_commission"],
        "win_rate": overall["win_rate"],
        "avg_pnl": overall["avg_pnl"],
        "avg_holding_minutes": (sum(holding) / len(holding)) if holding else None,
        "by_confidence": by_confidence,
        "by_exit_reason": by_exit_reason,
    }


def main() -> None:
    """CLI d'inspection : python -m trader.attribution [--json]."""
    import argparse

    parser = argparse.ArgumentParser(description="Attribution décision->résultat du trader paper")
    parser.add_argument("--json", action="store_true", help="sortie JSON compact")
    args = parser.parse_args()

    state_dir = Path(__file__).resolve().parent.parent / "state"
    attr = compute_attribution(state_dir)

    if args.json:
        print(json.dumps(attr, separators=(",", ":"), ensure_ascii=False))
        return

    def fmt(value: float | None, decimals: int = 2) -> str:
        return "n/a" if value is None else f"{value:.{decimals}f}"

    print("Attribution décision->résultat")
    print(f"Trades clôturés : {attr['n_closed_trades']}")
    print(f"P&L réalisé     : {fmt(attr['realized_pnl'])}")
    print(f"Win rate        : {fmt(attr['win_rate'], 3)}")
    print(f"P&L moyen/trade : {fmt(attr['avg_pnl'])}")
    print(f"Détention moy.  : {fmt(attr['avg_holding_minutes'])} min")
    if attr["by_confidence"]:
        print("Calibration confidence:")
        for bucket in attr["by_confidence"]:
            print(f"  {bucket['bucket']}: n={bucket['n']} win={fmt(bucket['win_rate'], 3)} "
                  f"pnl={fmt(bucket['total_pnl'])}")
    if attr["by_exit_reason"]:
        print("Par raison de sortie:")
        for reason in attr["by_exit_reason"]:
            print(f"  {reason['reason']}: n={reason['n']} pnl={fmt(reason['total_pnl'])}")


if __name__ == "__main__":
    main()
