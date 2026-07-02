"""Mesure d'attribution news×outcome — première analyse veto earnings.

Lit toutes les décisions annotées `news` depuis le 23/06 (archive juin +
decisions.jsonl live).  Pour chaque décision jugeable, calcule le rendement
forward (horizon 1d, fallback 4h, bande 0.005 — même paramétrage que
backtest/decision_quality.py) et buckétise par `earnings_in_h`.

Analyses :
  1. Trades exécutés (executed=True, BUY/SELL) — win rate par bucket earnings
  2. Toutes décisions scorables — même bucketing (HOLD pertinent ?)
  3. news_coverage/news_count vs outcome
  4. Volumétrie honnête avec flag n<10

Usage :
  uv run python scripts/news_attribution_measure.py
  uv run python scripts/news_attribution_measure.py --out state/archive/news-attribution-2026-07-02.json
  uv run ruff check scripts/news_attribution_measure.py
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
ARCHIVE = STATE / "archive"
DECISIONS_ARCHIVE = ARCHIVE / "decisions-2026-06.jsonl.gz"
DECISIONS_LIVE = STATE / "decisions.jsonl"
DEFAULT_OUT = ARCHIVE / "news-attribution-2026-07-02.json"

JUDGEABLE_REASONS = {"hold", "ok"}
HORIZONS = (("4h", timedelta(hours=4)), ("1d", timedelta(days=1)))
PRIMARY_HORIZON = "1d"
BAND = 0.005
LOW_N_THRESHOLD = 10

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chargement des décisions
# ---------------------------------------------------------------------------


def _get_news(row: dict) -> dict | None:
    """Extrait le payload news — top-level ou imbriqué dans `decision`."""
    news = row.get("news")
    if isinstance(news, dict):
        return news
    nested = row.get("decision")
    if isinstance(nested, dict):
        inner = nested.get("news")
        if isinstance(inner, dict):
            return inner
    return None


def load_decisions() -> list[dict]:
    """Charge toutes les décisions annotées news (archive juin + live)."""
    rows: list[dict] = []

    # Archive gzip juin
    if DECISIONS_ARCHIVE.exists():
        with gzip.open(DECISIONS_ARCHIVE, "rt", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and _get_news(obj):
                    rows.append(obj)
        log.info("Archive juin : %d décisions avec news", len(rows))

    # Ledger vif (juillet)
    live_before = len(rows)
    if DECISIONS_LIVE.exists():
        for raw in DECISIONS_LIVE.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and _get_news(obj):
                rows.append(obj)
    log.info("Ledger live : %d décisions avec news", len(rows) - live_before)
    log.info("Total décisions avec news : %d", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Scoring forward-return
# ---------------------------------------------------------------------------


def _load_history(decisions: list[dict]) -> object:
    """Charge le HistoryStore 1h pour tous les symboles couverts."""
    from backtest.data import DataError, HistoryStore  # noqa: PLC0415

    symbols = sorted({str(d["symbol"]) for d in decisions if d.get("symbol")})
    tss = [str(d["cycle_ts"]) for d in decisions if d.get("cycle_ts")]
    if tss:
        dates = [datetime.fromisoformat(ts.replace("Z", "+00:00")).date() for ts in tss]
        start = (min(dates) - timedelta(days=1)).isoformat()
        end = (max(dates) + timedelta(days=3)).isoformat()
    else:
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=12)).isoformat()
        end = today.isoformat()

    log.info("Chargement historique 1h : %d symboles (%s → %s)…", len(symbols), start, end)
    bars_by_symbol: dict[str, list] = {}
    unavailable: list[str] = []
    for sym in symbols:
        try:
            store = HistoryStore.load([sym], start, end, interval="1h")
            bars_by_symbol[sym] = list(store._bars_by_symbol.get(sym, []))  # noqa: SLF001
        except DataError as exc:
            log.warning("Symbole indisponible : %s (%s)", sym, exc)
            unavailable.append(sym)
    if unavailable:
        log.warning("Symboles sans historique (%d) : %s", len(unavailable), unavailable[:10])
    return HistoryStore.from_bars(bars_by_symbol)


def _forward_return_primary(history: object, symbol: str, ts: str) -> float | None:
    """Rendement forward 1d (fallback 4h si non évaluable)."""
    from backtest.decision_quality import forward_return  # noqa: PLC0415

    for label, delta in HORIZONS:
        fr = forward_return(history, symbol, ts, delta)
        if fr is not None:
            if label == PRIMARY_HORIZON:
                return fr
            # fallback : premier horizon disponible
            return fr
    return None


def _classify_and_verdict(action: str, fr: float | None) -> tuple[str, str]:
    """(classify_label, verdict) en cohérence avec decision_quality.py."""
    from backtest.decision_quality import classify  # noqa: PLC0415

    label = classify(action, fr, BAND)
    win_labels = {"gagnant", "bonne_prudence"}
    loss_labels = {"perdant", "opportunite_manquee"}
    neutral_labels = {"neutre", "justifie"}
    if label in win_labels:
        verdict = "WIN"
    elif label in loss_labels:
        verdict = "LOSS"
    elif label in neutral_labels:
        verdict = "NEUTRAL"
    else:
        verdict = "UNKNOWN"
    return label, verdict


# ---------------------------------------------------------------------------
# Bucketing earnings
# ---------------------------------------------------------------------------


def earnings_bucket(earnings_in_h: float | None) -> str:
    if earnings_in_h is None:
        return "null"
    if earnings_in_h < 24:
        return "<24h"
    if earnings_in_h < 72:
        return "24-72h"
    if earnings_in_h < 168:
        return "72-168h"
    return ">168h"


BUCKET_ORDER = ["<24h", "24-72h", "72-168h", ">168h", "null"]


# ---------------------------------------------------------------------------
# Agrégats par bucket
# ---------------------------------------------------------------------------


def _bucket_stats(rows: list[dict]) -> dict:
    """Calcule win rate, n, mean fwd return pour une liste de rows scorées."""
    n = len(rows)
    scorable = [r for r in rows if r["verdict"] != "UNKNOWN"]
    n_scorable = len(scorable)
    wins = [r for r in scorable if r["verdict"] == "WIN"]
    losses = [r for r in scorable if r["verdict"] == "LOSS"]
    neutrals = [r for r in scorable if r["verdict"] == "NEUTRAL"]
    frs = [r["forward_return"] for r in scorable if r["forward_return"] is not None]
    win_rate = round(len(wins) / n_scorable, 4) if n_scorable else None
    mean_fr = round(mean(frs), 6) if frs else None
    low_n = n_scorable < LOW_N_THRESHOLD
    return {
        "n": n,
        "n_scorable": n_scorable,
        "n_win": len(wins),
        "n_loss": len(losses),
        "n_neutral": len(neutrals),
        "n_unknown": n - n_scorable,
        "win_rate": win_rate,
        "mean_forward_return": mean_fr,
        "low_n_warning": low_n,
    }


def aggregate_by_bucket(scored: list[dict], filter_fn=None) -> dict:
    """Agrège les lignes scorées par bucket earnings, avec filtre optionnel."""
    subset = [r for r in scored if filter_fn is None or filter_fn(r)]
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in subset:
        by_bucket[r["earnings_bucket"]].append(r)
    result = {}
    for bkt in BUCKET_ORDER:
        rows_bkt = by_bucket.get(bkt, [])
        result[bkt] = _bucket_stats(rows_bkt)
    result["_total"] = _bucket_stats(subset)
    return result


# ---------------------------------------------------------------------------
# Analyse news_coverage / news_count
# ---------------------------------------------------------------------------


def aggregate_by_coverage(scored: list[dict]) -> dict:
    """Win rate par niveau de couverture news (empty / ok) et news_count quintiles."""
    by_coverage: dict[str, list[dict]] = defaultdict(list)
    for r in scored:
        by_coverage[r.get("news_coverage", "unknown")].append(r)

    coverage_stats = {}
    for cov, rows in sorted(by_coverage.items()):
        coverage_stats[cov] = _bucket_stats(rows)

    # news_count : 0, 1, 2-5, >5
    count_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in scored:
        nc = r.get("news_count", 0) or 0
        if nc == 0:
            lbl = "0"
        elif nc == 1:
            lbl = "1"
        elif nc <= 5:
            lbl = "2-5"
        else:
            lbl = ">5"
        count_buckets[lbl].append(r)

    count_stats = {}
    for lbl in ["0", "1", "2-5", ">5"]:
        count_stats[lbl] = _bucket_stats(count_buckets.get(lbl, []))

    return {"by_coverage": coverage_stats, "by_news_count": count_stats}


# ---------------------------------------------------------------------------
# Construction de la ligne scorée
# ---------------------------------------------------------------------------


def score_decisions(decisions: list[dict], history: object) -> list[dict]:
    """Produit une ligne scorée par décision jugeable."""
    scored: list[dict] = []
    for d in decisions:
        reason = str(d.get("reason") or "")
        if reason not in JUDGEABLE_REASONS:
            continue
        news = _get_news(d)
        if not news:
            continue
        symbol = str(d.get("symbol") or "")
        action = str(d.get("action") or "HOLD").upper()
        executed = bool(d.get("executed", False))
        cycle_ts = str(d.get("cycle_ts") or "")
        earnings_in_h = news.get("earnings_in_h")
        fr = _forward_return_primary(history, symbol, cycle_ts)
        classify_label, verdict = _classify_and_verdict(action, fr)
        scored.append(
            {
                "decision_id": d.get("decision_id"),
                "cycle_ts": cycle_ts,
                "symbol": symbol,
                "action": action,
                "executed": executed,
                "reason": reason,
                "earnings_in_h": earnings_in_h,
                "earnings_bucket": earnings_bucket(earnings_in_h),
                "news_coverage": news.get("news_coverage"),
                "news_count": news.get("news_count", 0),
                "news_source": news.get("source"),
                "forward_return": round(fr, 6) if fr is not None else None,
                "classify": classify_label,
                "verdict": verdict,
                "price": d.get("price"),
                "qty": d.get("qty"),
            }
        )
    log.info("Décisions jugeables scorées : %d", len(scored))
    return scored


# ---------------------------------------------------------------------------
# Entrée principale
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="chemin du fichier JSON de sortie")
    parser.add_argument("--verbose", "-v", action="store_true", help="logs DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    out_path = Path(args.out)

    # 1. Charger les décisions avec news
    decisions = load_decisions()

    # 2. Charger l'historique prix
    sys.path.insert(0, str(ROOT))
    judgeable = [d for d in decisions if d.get("reason") in JUDGEABLE_REASONS]
    history = _load_history(judgeable)

    # 3. Scorer
    scored = score_decisions(decisions, history)

    # 4. Filtres métier
    def is_executed_trade(r: dict) -> bool:
        return r["executed"] and r["action"] in ("BUY", "SELL")

    def is_hold(r: dict) -> bool:
        return r["action"] == "HOLD"

    # 5. Agrégats par bucket earnings
    analysis_executed = aggregate_by_bucket(scored, filter_fn=is_executed_trade)
    analysis_all = aggregate_by_bucket(scored)
    analysis_hold = aggregate_by_bucket(scored, filter_fn=is_hold)

    # 6. news_coverage / news_count
    coverage_analysis = aggregate_by_coverage(scored)

    # 7. Volumétrie globale
    total_with_news = len(decisions)
    n_judgeable = len(scored)
    n_executed_trades = sum(1 for r in scored if is_executed_trade(r))
    n_hold = sum(1 for r in scored if is_hold(r))
    verdict_counter = Counter(r["verdict"] for r in scored)
    bucket_counter = Counter(r["earnings_bucket"] for r in scored)
    action_counter = Counter(r["action"] for r in scored)

    # 8. Construire le rapport
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "primary_horizon": PRIMARY_HORIZON,
            "fallback_horizon": "4h",
            "band": BAND,
            "judgeable_reasons": sorted(JUDGEABLE_REASONS),
            "low_n_threshold": LOW_N_THRESHOLD,
            "sources": {
                "archive": str(DECISIONS_ARCHIVE),
                "live": str(DECISIONS_LIVE),
            },
        },
        "volumetry": {
            "total_decisions_with_news": total_with_news,
            "judgeable_scored": n_judgeable,
            "executed_trades": n_executed_trades,
            "hold_decisions": n_hold,
            "verdicts": dict(verdict_counter),
            "by_earnings_bucket": dict(bucket_counter),
            "by_action": dict(action_counter),
            "data_window_days": 10,
            "caveat": (
                "Fenêtre de 10 jours (23/06→02/07). "
                "Les buckets <24h et 24-72h ont n<10 trades exécutés — "
                "aucune conclusion statistique possible sur le veto earnings."
            ),
        },
        "analysis_1_executed_trades_by_earnings_bucket": analysis_executed,
        "analysis_2_all_decisions_by_earnings_bucket": analysis_all,
        "analysis_3_hold_decisions_by_earnings_bucket": analysis_hold,
        "analysis_4_news_coverage_vs_outcome": coverage_analysis,
    }

    # 9. Écriture
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Rapport écrit : %s", out_path)

    # 10. Résumé console
    print("\n=== NEWS ATTRIBUTION MEASURE — 2026-07-02 ===")
    print(f"Décisions avec news       : {total_with_news}")
    print(f"Jugeables scorées         : {n_judgeable}")
    print(f"Trades exécutés (BUY/SELL): {n_executed_trades}")
    print(f"HOLD jugeables            : {n_hold}")
    v = report["volumetry"]
    print(f"Verdicts                  : {dict(verdict_counter)}")
    print(f"Buckets earnings          : {dict(bucket_counter)}")
    print()
    print("--- Analyse 1 : Trades exécutés par bucket earnings ---")
    for bkt in BUCKET_ORDER:
        s = analysis_executed[bkt]
        wr = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "n/a"
        fr = f"{s['mean_forward_return']:+.3%}" if s["mean_forward_return"] is not None else "n/a"
        flag = " ⚠ n<10" if s["low_n_warning"] else ""
        print(f"  {bkt:<10}: n={s['n']:>4} scorable={s['n_scorable']:>4} win_rate={wr:>6} mean_fwd={fr:>8}{flag}")
    print()
    print("--- Analyse 2 : Toutes décisions jugeables par bucket earnings ---")
    for bkt in BUCKET_ORDER:
        s = analysis_all[bkt]
        wr = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "n/a"
        fr = f"{s['mean_forward_return']:+.3%}" if s["mean_forward_return"] is not None else "n/a"
        flag = " ⚠ n<10" if s["low_n_warning"] else ""
        print(f"  {bkt:<10}: n={s['n']:>4} scorable={s['n_scorable']:>4} win_rate={wr:>6} mean_fwd={fr:>8}{flag}")
    print()
    print("--- Analyse 4 : news_coverage vs outcome ---")
    for cov, stats in coverage_analysis["by_coverage"].items():
        wr = f"{stats['win_rate']:.1%}" if stats["win_rate"] is not None else "n/a"
        print(f"  coverage={cov:<8}: n={stats['n']:>5} win_rate={wr}")
    print("  news_count buckets:")
    for lbl in ["0", "1", "2-5", ">5"]:
        stats = coverage_analysis["by_news_count"].get(lbl, {})
        wr = f"{stats.get('win_rate', None):.1%}" if stats.get("win_rate") is not None else "n/a"
        print(f"    count={lbl:<5}: n={stats.get('n', 0):>5} win_rate={wr}")
    print()
    print(f"Rapport JSON : {out_path}")
    print()
    print("CAVEAT :", v["caveat"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
