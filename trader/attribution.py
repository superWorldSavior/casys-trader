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
from datetime import date, datetime, timezone
from pathlib import Path

# En-dessous, une position est considérée comme plate (évite les poussières
# flottantes après des clôtures décimales exactes qui créeraient de faux micro-trades).
_FLAT_EPS = 1e-9
_RECENT_TRIPS_LIMIT = 25

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
                "source_plan_id": row.get("source_plan_id"),
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


def _recent_round_trips(trips: list[dict], *, limit: int = _RECENT_TRIPS_LIMIT) -> list[dict]:
    """Retourne les derniers round-trips par heure de sortie décroissante."""
    if limit <= 0:
        return []
    return sorted(trips, key=lambda t: str(t["exit_ts"]), reverse=True)[:limit]


def _parse_iso_date(value: str, *, field: str) -> date:
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"{field} doit être une date/datetime ISO: {value!r}") from exc


def _normalize_excluded_symbols(exclude_symbols: tuple[str, ...] | frozenset[str]) -> tuple[str, ...]:
    raw_symbols = sorted(exclude_symbols) if isinstance(exclude_symbols, frozenset) else exclude_symbols
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in raw_symbols:
        parsed = str(symbol)
        if parsed in seen:
            continue
        normalized.append(parsed)
        seen.add(parsed)
    return tuple(normalized)


def _filter_regime_trips(
    trips: list[dict],
    *,
    since: str | None,
    exclude_symbols: tuple[str, ...] | frozenset[str],
) -> tuple[list[dict], dict]:
    since_date = _parse_iso_date(since, field="since") if since is not None else None
    excluded_symbols = _normalize_excluded_symbols(exclude_symbols)
    excluded_symbol_set = set(excluded_symbols)
    kept: list[dict] = []
    n_excluded_trades = 0

    for trip in trips:
        excluded = False
        if since_date is not None:
            exit_date = _parse_iso_date(str(trip.get("exit_ts")), field="exit_ts")
            excluded = exit_date < since_date
        if str(trip.get("symbol")) in excluded_symbol_set:
            excluded = True
        if excluded:
            n_excluded_trades += 1
        else:
            kept.append(trip)

    return kept, {
        "since": since,
        "excluded_symbols": list(excluded_symbols),
        "n_excluded_trades": n_excluded_trades,
    }


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bar_field(bar: object, field: str) -> object:
    if isinstance(bar, dict):
        return bar.get(field)
    return getattr(bar, field, None)


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _round_metric(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _future_bars_after_exit(bars: list[object], *, exit_ts: object, limit: int) -> list[dict]:
    exit_dt = _parse_optional_datetime(exit_ts)
    if exit_dt is None or limit <= 0:
        return []

    parsed_bars: list[dict] = []
    for bar in bars:
        ts_raw = _bar_field(bar, "ts")
        ts = _parse_optional_datetime(ts_raw)
        high = _finite_float(_bar_field(bar, "high"))
        low = _finite_float(_bar_field(bar, "low"))
        close = _finite_float(_bar_field(bar, "close"))
        if ts is None or high is None or low is None or close is None:
            continue
        if ts <= exit_dt:
            continue
        parsed_bars.append(
            {
                "ts": str(ts_raw),
                "dt": ts,
                "high": high,
                "low": low,
                "close": close,
            }
        )

    parsed_bars.sort(key=lambda row: row["dt"])
    return parsed_bars[:limit]


def _position_pnl(
    *,
    side: str,
    entry_price: float,
    quantity: float,
    price: float,
    commission: float,
) -> float:
    sign = 1.0 if side == "LONG" else -1.0
    return (price - entry_price) * quantity * sign - commission


def _diagnose_hard_stop_trip(trip: dict, bars: list[object], *, lookahead_bars: int) -> dict:
    symbol = str(trip.get("symbol"))
    side = str(trip.get("side"))
    quantity = float(trip.get("quantity") or 0.0)
    entry_price = float(trip.get("entry_price") or 0.0)
    exit_price = float(trip.get("exit_price") or 0.0)
    actual_pnl = float(trip.get("pnl") or 0.0)
    commission = float(trip.get("commission") or 0.0)
    future_bars = _future_bars_after_exit(bars, exit_ts=trip.get("exit_ts"), limit=lookahead_bars)

    base = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "entry_ts": trip.get("entry_ts"),
        "exit_ts": trip.get("exit_ts"),
        "entry_price": _round_metric(entry_price),
        "exit_price": _round_metric(exit_price),
        "actual_pnl": _round_metric(actual_pnl),
        "commission": _round_metric(commission),
        "source_plan_id": trip.get("source_plan_id"),
        "future_bars": len(future_bars),
    }

    if not future_bars or quantity <= 0.0 or entry_price <= 0.0 or side not in ("LONG", "SHORT"):
        return {
            **base,
            "verdict": "unknown_no_future_bars",
            "hold_to_lookahead_pnl": None,
            "hold_to_lookahead_delta_vs_actual": None,
            "best_after_stop_pnl": None,
            "worst_after_stop_pnl": None,
            "recovered_to_entry": None,
            "would_have_won_by_lookahead": None,
            "would_have_beaten_stop_by_lookahead": None,
        }

    lookahead_close = future_bars[-1]["close"]
    if side == "LONG":
        best_price = max(bar["high"] for bar in future_bars)
        worst_price = min(bar["low"] for bar in future_bars)
        recovered_to_entry = best_price >= entry_price
    else:
        best_price = min(bar["low"] for bar in future_bars)
        worst_price = max(bar["high"] for bar in future_bars)
        recovered_to_entry = best_price <= entry_price

    hold_to_lookahead_pnl = _position_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=lookahead_close,
        commission=commission,
    )
    best_after_stop_pnl = _position_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=best_price,
        commission=commission,
    )
    worst_after_stop_pnl = _position_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=worst_price,
        commission=commission,
    )
    hold_delta = hold_to_lookahead_pnl - actual_pnl

    return {
        **base,
        "verdict": "stop_too_early" if hold_delta > 0.0 else "stop_helped_or_neutral",
        "lookahead_last_ts": future_bars[-1]["ts"],
        "lookahead_close": _round_metric(lookahead_close),
        "hold_to_lookahead_pnl": _round_metric(hold_to_lookahead_pnl),
        "hold_to_lookahead_delta_vs_actual": _round_metric(hold_delta),
        "best_after_stop_price": _round_metric(best_price),
        "best_after_stop_pnl": _round_metric(best_after_stop_pnl),
        "best_after_stop_delta_vs_actual": _round_metric(best_after_stop_pnl - actual_pnl),
        "worst_after_stop_price": _round_metric(worst_price),
        "worst_after_stop_pnl": _round_metric(worst_after_stop_pnl),
        "worst_after_stop_delta_vs_actual": _round_metric(worst_after_stop_pnl - actual_pnl),
        "recovered_to_entry": recovered_to_entry,
        "would_have_won_by_lookahead": hold_to_lookahead_pnl > 0.0,
        "would_have_beaten_stop_by_lookahead": hold_to_lookahead_pnl > actual_pnl,
    }


def compute_hard_stop_diagnostics(
    state_dir: Path,
    bars_by_symbol: dict[str, list[object]],
    *,
    since: str | None = None,
    exclude_symbols: tuple[str, ...] | frozenset[str] = (),
    lookahead_bars: int = 8,
    interval: str = "1h",
) -> dict:
    """Diagnostique après coup les sorties `hard_stop`.

    Pour chaque hard stop, compare le P&L réalisé au P&L contrefactuel si la
    position avait été gardée jusqu'à la dernière bougie disponible de la fenêtre.
    Le calcul reste purement prix: aucune comparaison de modèle, aucun appel LLM.
    """
    trips = compute_round_trips(state_dir)
    trips, regime = _filter_regime_trips(
        trips,
        since=since,
        exclude_symbols=exclude_symbols,
    )
    hard_stop_trips = [trip for trip in trips if trip.get("exit_reason") == "hard_stop"]
    cases = [
        _diagnose_hard_stop_trip(
            trip,
            list(bars_by_symbol.get(str(trip.get("symbol")), [])),
            lookahead_bars=lookahead_bars,
        )
        for trip in hard_stop_trips
    ]
    diagnosed = [case for case in cases if case["hold_to_lookahead_pnl"] is not None]

    actual_pnl = sum(float(case["actual_pnl"] or 0.0) for case in cases)
    diagnosed_actual_pnl = sum(float(case["actual_pnl"] or 0.0) for case in diagnosed)
    hold_to_lookahead_pnl = sum(float(case["hold_to_lookahead_pnl"] or 0.0) for case in diagnosed)
    best_after_stop_pnl = sum(float(case["best_after_stop_pnl"] or 0.0) for case in diagnosed)
    worst_after_stop_pnl = sum(float(case["worst_after_stop_pnl"] or 0.0) for case in diagnosed)

    return {
        "lookahead_bars": lookahead_bars,
        "interval": interval,
        "summary": {
            "hard_stops": len(cases),
            "diagnosed": len(diagnosed),
            "unknown": len(cases) - len(diagnosed),
            "stop_too_early": sum(1 for case in cases if case["verdict"] == "stop_too_early"),
            "stop_helped_or_neutral": sum(1 for case in cases if case["verdict"] == "stop_helped_or_neutral"),
            "actual_pnl": _round_metric(actual_pnl),
            "diagnosed_actual_pnl": _round_metric(diagnosed_actual_pnl),
            "hold_to_lookahead_pnl": _round_metric(hold_to_lookahead_pnl),
            "hold_to_lookahead_delta_vs_actual": _round_metric(hold_to_lookahead_pnl - diagnosed_actual_pnl),
            "best_after_stop_pnl": _round_metric(best_after_stop_pnl),
            "best_after_stop_delta_vs_actual": _round_metric(best_after_stop_pnl - diagnosed_actual_pnl),
            "worst_after_stop_pnl": _round_metric(worst_after_stop_pnl),
            "worst_after_stop_delta_vs_actual": _round_metric(worst_after_stop_pnl - diagnosed_actual_pnl),
        },
        "cases": sorted(cases, key=lambda case: str(case.get("exit_ts")), reverse=True),
        "regime": regime,
    }


def compute_attribution(
    state_dir: Path,
    *,
    since: str | None = None,
    exclude_symbols: tuple[str, ...] | frozenset[str] = (),
) -> dict:
    """Round-trips + calibration par bucket de confidence + breakdown raison de sortie.

    Sortie machine-readable compacte, injectable dans le contexte de l'agent pour
    qu'il s'auto-corrige (ses calls confiants gagnent-ils ? quelles sorties coûtent ?).
    """
    trips = compute_round_trips(state_dir)
    trips, regime = _filter_regime_trips(
        trips,
        since=since,
        exclude_symbols=exclude_symbols,
    )
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
        "recent_trips": _recent_round_trips(trips),
        "regime": regime,
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
