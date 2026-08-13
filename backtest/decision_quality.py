"""Scoring de qualité des décisions par rendement forward, sans replay P&L."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trader.domain.learnings.scoring import (
    SIGNIFICANT_RETURN_BAND,
    classify_decision_quality,
)
from trader.domain.decision_benchmark import (
    BENCHMARK_SEMANTICS_VERSION,
    decision_verdict,
)
from trader.domain.market_data import Bar
from trader.infrastructure.files.ledger_rotation import read_rows_with_archive

from .data import DataError, HistoryStore

HORIZONS = (("4h", timedelta(hours=4)), ("1d", timedelta(days=1)))
BAND = SIGNIFICANT_RETURN_BAND
JUDGEABLE_REASONS = {"hold", "ok"}
SCORABLE_VERDICTS = {"good", "bad", "neutral", "missed"}
STATE_DIR = Path("state")

CAVEAT = (
    "Caveat : fenêtre courte + week-end => beaucoup de non-évaluables ; "
    "un HOLD sans direction explicite reste inconnu si le mouvement est matériel."
)


class AggregateResult(list[dict]):
    """Liste action×horizon enrichie avec la section provider."""

    def __init__(self, aggregate_rows: list[dict], by_provider: list[dict]):
        super().__init__(aggregate_rows)
        self.by_provider = by_provider

    def __getitem__(self, key: int | slice | str) -> Any:
        if key == "aggregate":
            return list(self)
        if key == "by_provider":
            return self.by_provider
        return super().__getitem__(key)


def classify(action: str, forward_return: float | None, band: float) -> str:
    """Conserve l'API historique action-only pour les appelants externes.

    Le bench du ledger passe par :func:`decision_verdict`, car une action HOLD
    seule ne permet pas de savoir si l'opportunité refusée était long ou short.
    """
    return classify_decision_quality(action, forward_return, band)


def forward_return(history: HistoryStore, symbol: str, ts: str, horizon: timedelta) -> float | None:
    """Rendement entre le close as-of à `ts` et le close postérieur à l'horizon."""
    p0 = history.price_asof(symbol, ts)
    ph = history.price_after(symbol, ts, horizon)
    if p0 is None or p0 == 0 or ph is None:
        return None
    return ph / p0 - 1.0


def score_decisions(decisions: list[dict], history: HistoryStore, band: float) -> list[dict]:
    """Produit une ligne de scoring par décision jugeable et par horizon."""
    rows: list[dict] = []
    for decision in decisions:
        action = str(decision["action"]).upper()
        symbol = str(decision["symbol"])
        cycle_ts = str(decision["cycle_ts"])
        provider = str(decision.get("llm_provider") or "unknown")
        fallback_reason = decision.get("llm_fallback_reason")
        for horizon_label, horizon in HORIZONS:
            rendement = forward_return(history, symbol, cycle_ts, horizon)
            verdict, benchmark_context = decision_verdict(decision, rendement, band)
            rows.append(
                {
                    "decision_id": decision.get("decision_id"),
                    "cycle_ts": cycle_ts,
                    "symbol": symbol,
                    "action": action,
                    "provider": provider,
                    "fallback_reason": fallback_reason,
                    "horizon": horizon_label,
                    "forward_return": rendement,
                    "evaluable": rendement is not None,
                    "scorable": verdict in SCORABLE_VERDICTS,
                    "verdict": verdict,
                    "benchmark_semantics_version": BENCHMARK_SEMANTICS_VERSION,
                    **benchmark_context,
                }
            )
    return rows


def aggregate(rows: list[dict]) -> AggregateResult:
    """Agrège les verdicts par couple `(action, horizon)` et par provider."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row["action"]).upper(), str(row["horizon"]))
        grouped.setdefault(key, []).append(row)

    result: list[dict] = []
    for action, horizon in sorted(grouped, key=_aggregate_sort_key):
        group = grouped[(action, horizon)]
        evaluables = [row for row in group if row["evaluable"]]
        scorables = [row for row in group if _is_scorable(row)]
        returns = [float(row["forward_return"]) for row in evaluables]
        buckets = dict(sorted(Counter(str(row["verdict"]) for row in group).items()))
        n_evaluable = len(evaluables)
        n_scorable = len(scorables)

        hit_rate = None
        if action in {"BUY", "SELL"} and n_scorable:
            hit_rate = buckets.get("good", 0) / n_scorable

        frileux_rate = None
        if action == "HOLD" and n_scorable:
            frileux_rate = buckets.get("missed", 0) / n_scorable

        result.append(
            {
                "action": action,
                "horizon": horizon,
                "n_total": len(group),
                "n_evaluable": n_evaluable,
                "n_non_evaluable": len(group) - n_evaluable,
                "n_scorable": n_scorable,
                "n_unscorable": len(group) - n_scorable,
                "n_unknown": buckets.get("unknown", 0),
                "n_machine": buckets.get("machine", 0),
                "coverage_pct": _coverage_pct(n_scorable, len(group)),
                "mean_forward_return": (sum(returns) / n_evaluable) if n_evaluable else None,
                "buckets": buckets,
                "hit_rate": hit_rate,
                "frileux_rate": frileux_rate,
            }
        )
    return AggregateResult(result, _aggregate_by_provider(rows))


def _is_scorable(row: dict[str, Any]) -> bool:
    """Tolère les anciennes lignes sans champ ``scorable`` explicite."""
    value = row.get("scorable")
    if isinstance(value, bool):
        return value
    return str(row.get("verdict") or "") in SCORABLE_VERDICTS


def _coverage_pct(n_scorable: int, n_total: int) -> float | None:
    if n_total <= 0:
        return None
    return round((n_scorable / n_total) * 100.0, 2)


def _aggregate_by_provider(rows: list[dict]) -> list[dict]:
    """Agrège les lignes scorées par provider LLM."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        provider = str(row.get("provider") or "unknown")
        grouped.setdefault(provider, []).append(row)

    result: list[dict] = []
    for provider in sorted(grouped):
        group = grouped[provider]
        evaluables = [row for row in group if row["evaluable"]]
        scorables = [row for row in group if _is_scorable(row)]
        returns = [float(row["forward_return"]) for row in evaluables]
        actions = Counter(str(row["action"]).upper() for row in group)
        buckets = dict(sorted(Counter(str(row["verdict"]) for row in group).items()))
        n_evaluable = len(evaluables)
        n_scorable = len(scorables)

        result.append(
            {
                "provider": provider,
                "n_total": len(group),
                "n_evaluable": n_evaluable,
                "n_non_evaluable": len(group) - n_evaluable,
                "n_scorable": n_scorable,
                "n_unscorable": len(group) - n_scorable,
                "n_unknown": buckets.get("unknown", 0),
                "n_machine": buckets.get("machine", 0),
                "coverage_pct": _coverage_pct(n_scorable, len(group)),
                "mean_forward_return": (sum(returns) / n_evaluable) if n_evaluable else None,
                "actions": {action: actions.get(action, 0) for action in ("BUY", "HOLD", "SELL")},
                "buckets": buckets,
            }
        )
    return result


def _aggregate_sort_key(item: tuple[str, str]) -> tuple[int, int, str, str]:
    action, horizon = item
    action_rank = {"BUY": 0, "HOLD": 1, "SELL": 2}.get(action, 99)
    horizon_rank = {label: index for index, (label, _delta) in enumerate(HORIZONS)}.get(horizon, 99)
    return action_rank, horizon_rank, action, horizon


def _partition_decisions(decisions: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    """Sépare les décisions jugeables et les exclusions comptées par raison."""
    judgeable: list[dict] = []
    excluded = Counter()
    for decision in decisions:
        reason = str(decision.get("reason") or "missing_reason")
        if reason in JUDGEABLE_REASONS:
            judgeable.append(decision)
        else:
            excluded[reason] += 1

    stale = excluded.get("stale_market_data", 0)
    gates = sum(count for reason, count in excluded.items() if reason != "stale_market_data")
    return judgeable, {
        "stale_market_data": stale,
        "gates": gates,
        "by_reason": dict(sorted(excluded.items())),
    }


def _date_window(decisions: list[dict], days_buffer: int) -> tuple[str, str] | tuple[None, None]:
    """Calcule la fenêtre de prix autour des timestamps du set jugeable."""
    if not decisions:
        return None, None

    dates = [datetime.fromisoformat(str(decision["cycle_ts"])).date() for decision in decisions]
    start = min(dates) - timedelta(days=days_buffer)
    end = max(dates) + timedelta(days=2)
    return start.isoformat(), end.isoformat()


def _load_available_history(
    symbols: list[str],
    start: str | None,
    end: str | None,
    interval: str,
) -> tuple[HistoryStore, list[str]]:
    """Charge chaque symbole séparément pour isoler les erreurs data."""
    if start is None or end is None:
        return HistoryStore.from_bars({}), []

    bars_by_symbol: dict[str, list[Bar]] = {}
    unavailable: list[str] = []
    for symbol in symbols:
        try:
            symbol_history = HistoryStore.load([symbol], start, end, interval=interval)
        except DataError:
            unavailable.append(symbol)
            continue
        bars_by_symbol[symbol] = list(symbol_history._bars_by_symbol.get(symbol, ()))
    return HistoryStore.from_bars(bars_by_symbol), unavailable


def score_from_ledger(
    path: Path | str,
    *,
    band: float = BAND,
    days_buffer: int = 1,
    interval: str = "1h",
    archive_dir: Path | None = None,
) -> dict:
    """Score les décisions jugeables d'un ledger avec une clé de jointure stable.

    Si ``archive_dir`` est fourni (ou inféré depuis ``path``), les archives gzip
    mensuelles sont chaînées avant le fichier vif pour couvrir l'historique complet.
    """
    ledger_path = Path(path)
    _archive = archive_dir if archive_dir is not None else ledger_path.parent / "archive"
    decisions = list(read_rows_with_archive(ledger_path, _archive))
    judgeable, exclusions = _partition_decisions(decisions)
    symbols = sorted({str(decision["symbol"]) for decision in judgeable})
    start, end = _date_window(judgeable, days_buffer)
    history, unavailable = _load_available_history(symbols, start, end, interval)
    scored_rows = score_decisions(judgeable, history, band)
    return {
        "scored_rows": scored_rows,
        "judgeable": judgeable,
        "exclusions": exclusions,
        "window": (start, end),
        "ledger_rows": decisions,
        "symbols": symbols,
        "unavailable_symbols": unavailable,
    }


def _render_cli(report: dict[str, Any]) -> str:
    """Rendu texte déterministe pour lecture humaine."""
    lines = [
        "Qualité des décisions vs rendement forward",
        (
            f"Ledger: {report['ledger']} | band={report['params']['band']:.4f} | "
            f"interval={report['params']['interval']} | fenêtre={report['params']['start']}→{report['params']['end']}"
        ),
        (
            f"Décisions jugeables: {report['counts']['judgeable']} / {report['counts']['ledger']} | "
            f"lignes scorées: {report['counts']['rows']}"
        ),
        "",
        f"{'Action':<6} {'Hz':<3} {'Taux':<14} {'Mean fwd':>10} {'Score':>6} {'Couv.':>7} "
        f"{'Eval':>6} {'Non-éval':>9}  Buckets",
        "-" * 94,
    ]

    for item in report["aggregate"]:
        action = item["action"]
        rate_label, rate = _display_rate(item)
        lines.append(
            f"{action:<6} {item['horizon']:<3} {rate_label:<7} {_format_rate(rate):>6} "
            f"{_format_pct(item['mean_forward_return']):>10} {item['n_scorable']:>6} "
            f"{_format_percentage(item['coverage_pct']):>7} {item['n_evaluable']:>6} "
            f"{item['n_non_evaluable']:>9}  {_format_buckets(item['buckets'])}"
        )

    if not report["aggregate"]:
        lines.append("(aucune décision jugeable)")

    lines.extend(
        [
            "",
            "Par provider",
            f"{'Provider':<16} {'n':>5} {'Score':>5} {'Couv.':>7} {'Mean fwd':>10}  Mix actions",
            "-" * 76,
        ]
    )
    for item in report["by_provider"]:
        lines.append(
            f"{item['provider']:<16} {item['n_total']:>5} {item['n_scorable']:>5} "
            f"{_format_percentage(item['coverage_pct']):>7} "
            f"{_format_pct(item['mean_forward_return']):>10}  {_format_actions(item['actions'])}"
        )
    if not report["by_provider"]:
        lines.append("(aucun provider)")

    exclusions = report["exclusions"]
    lines.extend(
        [
            "",
            f"Exclus : {exclusions['stale_market_data']} stale_market_data, {exclusions['gates']} gates",
        ]
    )
    if exclusions["by_reason"]:
        lines.append(f"Détail exclusions : {_format_buckets(exclusions['by_reason'])}")
    if report["unavailable_symbols"]:
        lines.append(f"Symboles indisponibles : {', '.join(report['unavailable_symbols'])}")
    lines.append(CAVEAT)
    lines.append(f"Rapport complet → {report['output_path']}")
    return "\n".join(lines)


def _display_rate(item: dict[str, Any]) -> tuple[str, float | None]:
    """Choisit le taux métier pertinent pour l'action."""
    if item["action"] == "HOLD":
        return "frileux", item["frileux_rate"]
    if item["action"] in {"BUY", "SELL"}:
        return "hit", item["hit_rate"]
    return "n/a", None


def _format_rate(value: float | None) -> str:
    """Formate un taux optionnel en pourcentage."""
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _format_percentage(value: float | None) -> str:
    """Formate un pourcentage déjà exprimé sur l'échelle 0–100."""
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def _format_pct(value: float | None) -> str:
    """Formate un rendement optionnel en pourcentage signé."""
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f}%"


def _format_buckets(buckets: dict[str, int]) -> str:
    """Formate des buckets triés sous forme compacte."""
    return ", ".join(f"{name}={count}" for name, count in sorted(buckets.items()))


def _format_actions(actions: dict[str, int]) -> str:
    """Formate la ventilation d'actions en ordre stable."""
    return ", ".join(f"{action}={actions.get(action, 0)}" for action in ("BUY", "HOLD", "SELL"))


def _build_report(args: argparse.Namespace) -> dict[str, Any]:
    """Orchestre les I/O du CLI autour des fonctions pures."""
    ledger_path = Path(args.ledger)
    data = score_from_ledger(
        ledger_path,
        band=args.band,
        days_buffer=args.days_buffer,
        interval=args.interval,
    )
    start, end = data["window"]
    rows = data["scored_rows"]
    stats = aggregate(rows)

    output_path = STATE_DIR / "last_decision_quality.json"
    return {
        "params": {
            "benchmark_semantics_version": BENCHMARK_SEMANTICS_VERSION,
            "band": args.band,
            "interval": args.interval,
            "days_buffer": args.days_buffer,
            "horizons": [label for label, _horizon in HORIZONS],
            "start": start,
            "end": end,
        },
        "ledger": str(ledger_path),
        "counts": {
            "ledger": len(data["ledger_rows"]),
            "judgeable": len(data["judgeable"]),
            "rows": len(rows),
            "scorable": sum(1 for row in rows if _is_scorable(row)),
            "coverage_pct": _coverage_pct(
                sum(1 for row in rows if _is_scorable(row)),
                len(rows),
            ),
        },
        "symbols": data["symbols"],
        "unavailable_symbols": data["unavailable_symbols"],
        "exclusions": data["exclusions"],
        "aggregate": stats["aggregate"],
        "by_provider": stats["by_provider"],
        "rows": rows,
        "output_path": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scoring de qualité des décisions vs rendement forward")
    parser.add_argument("--ledger", default="state/decisions.jsonl", help="ledger JSONL des décisions")
    parser.add_argument("--band", type=float, default=BAND, help="bande significative de rendement forward")
    parser.add_argument("--interval", default="1h", help="intervalle de prix historiques")
    parser.add_argument("--days-buffer", type=int, default=1, help="jours de marge avant la première décision")
    parser.add_argument("--json", action="store_true", help="affiche le rapport complet en JSON")
    args = parser.parse_args()

    report = _build_report(args)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    Path(report["output_path"]).write_text(json.dumps(report, ensure_ascii=False, indent=2))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_cli(report))


if __name__ == "__main__":
    main()
