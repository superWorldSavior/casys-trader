"""Bootstrap des outcome_scores FLAIR pour les learnings historiques.

Algorithme V1 (1 learning = 1 decision_id = 1 apparition) :
  outcome_score = (nb_wins - nb_losses) / nb_apparitions
  → pour V1 : WIN = +1, LOSS = -1, NEUTRAL = 0, UNKNOWN = non évalué.

Sources :
  - state/archive/learnings-from-ledger.jsonl (1 867 learnings archivés)
  - state/learnings.jsonl (buffer vif, dédupliqué)

Join : chaque learning porte un decision_id → jointure avec l'archive des
décisions (state/archive/decisions-*.jsonl.gz + state/decisions.jsonl).
Score via forward_return aux horizons 4h et 1d (même paramétrage que
backtest/decision_quality.py), verdict primaire = 1d (fallback 4h).

Mapping verdict :
  gagnant / bonne_prudence → WIN
  perdant / opportunite_manquee → LOSS
  neutre / justifie → NEUTRAL
  non_evaluable / inconnu → UNKNOWN

Usage :
  uv run python scripts/learnings_outcome_bootstrap.py [--verbose]
  uv run python scripts/learnings_outcome_bootstrap.py --out state/archive/learnings-outcome-bootstrap.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
ARCHIVE = STATE / "archive"
LEARNINGS_ARCHIVE = ARCHIVE / "learnings-from-ledger.jsonl"
LEARNINGS_LIVE = STATE / "learnings.jsonl"
DECISIONS_LIVE = STATE / "decisions.jsonl"
DEFAULT_OUT = ARCHIVE / "learnings-outcome-bootstrap.json"

NOTE_TRUNC = 120

# Horizons identiques à backtest/decision_quality.py
_HORIZONS = (("4h", timedelta(hours=4)), ("1d", timedelta(days=1)))
_PRIMARY_HORIZON = "1d"
_BAND = 0.005

# Verdicts positifs / négatifs au sens FLAIR
_WIN_VERDICTS = {"gagnant", "bonne_prudence"}
_LOSS_VERDICTS = {"perdant", "opportunite_manquee"}
_NEUTRAL_VERDICTS = {"neutre", "justifie"}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chargement des sources
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
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
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def load_learnings() -> list[dict]:
    """Charge et déduplique les learnings archive + buffer vif.

    Clé de déduplication :
    - Archive : decision_id (toujours présent dans learnings-from-ledger.jsonl)
    - Buffer vif : (ts, symbol) — pas de decision_id dans le buffer actuel
    """
    seen_decision_ids: set[str] = set()
    seen_ts_symbol: set[tuple[str, str]] = set()
    out: list[dict] = []

    for row in _read_jsonl(LEARNINGS_ARCHIVE):
        did = str(row.get("decision_id") or "")
        if did:
            if did in seen_decision_ids:
                continue
            seen_decision_ids.add(did)
        else:
            key = (str(row.get("ts") or ""), str(row.get("symbol") or ""))
            if key in seen_ts_symbol:
                continue
            seen_ts_symbol.add(key)
        out.append(row)

    live_added = 0
    for row in _read_jsonl(LEARNINGS_LIVE):
        did = str(row.get("decision_id") or "")
        ts = str(row.get("ts") or "")
        sym = str(row.get("symbol") or "")
        if did and did in seen_decision_ids:
            continue
        key = (ts, sym)
        if key in seen_ts_symbol:
            continue
        seen_ts_symbol.add(key)
        out.append(row)
        live_added += 1

    log.info("Learnings chargés : %d archive + %d vif = %d total", len(out) - live_added, live_added, len(out))
    return out


def load_decisions() -> dict[str, dict]:
    """Charge toutes les décisions et les indexe par decision_id."""
    from trader.ledger_rotation import read_rows_with_archive  # noqa: PLC0415

    by_id: dict[str, dict] = {}
    for row in read_rows_with_archive(DECISIONS_LIVE, ARCHIVE):
        did = row.get("decision_id")
        if did:
            by_id[str(did)] = row
    log.info("Décisions indexées : %d", len(by_id))
    return by_id


def _build_ts_symbol_index(decisions: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """Index secondaire (cycle_ts, symbol) pour les learnings sans decision_id."""
    idx: dict[tuple[str, str], dict] = {}
    for row in decisions.values():
        cts = str(row.get("cycle_ts") or "")
        sym = str(row.get("symbol") or "")
        if cts and sym:
            idx[(cts, sym)] = row
    return idx


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _classify_raw(action: str, forward_ret: float | None) -> str:
    from backtest.decision_quality import classify  # noqa: PLC0415

    return classify(action, forward_ret, _BAND)


def _verdict(classify_result: str) -> str:
    if classify_result in _WIN_VERDICTS:
        return "WIN"
    if classify_result in _LOSS_VERDICTS:
        return "LOSS"
    if classify_result in _NEUTRAL_VERDICTS:
        return "NEUTRAL"
    return "UNKNOWN"


def _forward_return(history: object, symbol: str, ts: str, horizon: timedelta) -> float | None:
    from backtest.decision_quality import forward_return  # noqa: PLC0415

    return forward_return(history, symbol, ts, horizon)  # type: ignore[arg-type]


def score_learning(
    learning: dict,
    decision: dict | None,
    history: object,
) -> dict:
    """Produit la ligne scorée pour un learning."""
    ts = str(learning.get("ts") or "")
    symbol = str(learning.get("symbol") or "")
    note_raw = str(learning.get("note") or "")
    action = str((decision or learning).get("action") or learning.get("action") or "HOLD").upper()
    executed = bool(learning.get("executed", False))
    decision_id = str(learning.get("decision_id") or (decision or {}).get("decision_id") or "")

    # Utilise le cycle_ts de la décision si disponible (plus précis que le ts du learning)
    score_ts = str((decision or {}).get("cycle_ts") or ts)

    fr: dict[str, float | None] = {}
    cl: dict[str, str] = {}
    for label, delta in _HORIZONS:
        fr[label] = _forward_return(history, symbol, score_ts, delta)
        cl[label] = _classify_raw(action, fr[label])

    # Verdict primaire = 1d ; fallback 4h si non_evaluable
    primary_classify = cl[_PRIMARY_HORIZON]
    primary_fr = fr[_PRIMARY_HORIZON]
    if primary_classify == "non_evaluable":
        # fallback au premier autre horizon
        other_label = next(lbl for lbl, _ in _HORIZONS if lbl != _PRIMARY_HORIZON)
        primary_classify = cl[other_label]
        primary_fr = fr[other_label]

    return {
        "decision_id": decision_id,
        "ts": ts,
        "symbol": symbol,
        "note": note_raw[:NOTE_TRUNC],
        "action": action,
        "executed": executed,
        "verdict": _verdict(primary_classify),
        "classify_primary": primary_classify,
        "forward_return": round(primary_fr, 6) if primary_fr is not None else None,
        "classify_4h": cl["4h"],
        "classify_1d": cl["1d"],
        "forward_return_4h": round(fr["4h"], 6) if fr["4h"] is not None else None,
        "forward_return_1d": round(fr["1d"], 6) if fr["1d"] is not None else None,
    }


# ---------------------------------------------------------------------------
# Chargement de l'historique prix
# ---------------------------------------------------------------------------


def _date_range(learnings: list[dict]) -> tuple[str, str]:
    tss = [str(row.get("ts") or "") for row in learnings if row.get("ts")]
    if not tss:
        today = datetime.now(timezone.utc).date()
        return (today - timedelta(days=2)).isoformat(), today.isoformat()
    min_ts = min(tss)
    max_ts = max(tss)
    start_dt = datetime.fromisoformat(min_ts.replace("Z", "+00:00")).date() - timedelta(days=1)
    end_dt = datetime.fromisoformat(max_ts.replace("Z", "+00:00")).date() + timedelta(days=3)
    return start_dt.isoformat(), end_dt.isoformat()


def load_history(symbols: list[str], start: str, end: str) -> tuple[object, list[str]]:
    """Charge l'historique 1h pour la liste de symboles; renvoie (HistoryStore, unavailable)."""
    from backtest.data import DataError, HistoryStore  # noqa: PLC0415

    bars_by_symbol: dict[str, list] = {}
    unavailable: list[str] = []
    for sym in sorted(set(symbols)):
        try:
            store = HistoryStore.load([sym], start, end, interval="1h")
            bars_by_symbol[sym] = list(store._bars_by_symbol.get(sym, []))  # noqa: SLF001
        except DataError as exc:
            log.warning("Symbole indisponible : %s (%s)", sym, exc)
            unavailable.append(sym)
    return HistoryStore.from_bars(bars_by_symbol), unavailable


# ---------------------------------------------------------------------------
# Agrégats
# ---------------------------------------------------------------------------


def _symbol_family(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("=X"):
        return "FX"
    if s.endswith("=F"):
        return "Futures"
    if s.endswith(".TW"):
        return "TW"
    if s.endswith(".PA"):
        return "EU_PA"
    if s.endswith(".SW"):
        return "EU_SW"
    if s.endswith(".AS"):
        return "EU_AS"
    if s.startswith("^"):
        return "Index"
    return "US"


def _win_rate(rows: list[dict]) -> float | None:
    scorable = [r for r in rows if r["verdict"] != "UNKNOWN"]
    if not scorable:
        return None
    wins = sum(1 for r in scorable if r["verdict"] == "WIN")
    return round(wins / len(scorable), 4)


def compute_aggregates(scored: list[dict]) -> dict:
    """Calcule les agrégats globaux et par segment."""
    total = len(scored)
    by_verdict: Counter[str] = Counter(r["verdict"] for r in scored)
    scorable = [r for r in scored if r["verdict"] != "UNKNOWN"]

    executed_rows = [r for r in scored if r["executed"]]
    hold_rows = [r for r in scored if not r["executed"]]
    buy_sell_rows = [r for r in scored if r["action"] in ("BUY", "SELL")]

    # Top-10 notes WIN / LOSS (exemples pour le futur recall)
    wins = [r for r in scorable if r["verdict"] == "WIN"]
    losses = [r for r in scorable if r["verdict"] == "LOSS"]
    top_win_notes = [{"symbol": r["symbol"], "action": r["action"], "note": r["note"]} for r in wins[:10]]
    top_loss_notes = [{"symbol": r["symbol"], "action": r["action"], "note": r["note"]} for r in losses[:10]]

    # Par symbole
    by_symbol_rows: dict[str, list[dict]] = defaultdict(list)
    for r in scored:
        by_symbol_rows[r["symbol"]].append(r)

    by_symbol = sorted(
        [
            {
                "symbol": sym,
                "n": len(rows),
                "scorable": sum(1 for r in rows if r["verdict"] != "UNKNOWN"),
                "win_rate": _win_rate(rows),
                "family": _symbol_family(sym),
            }
            for sym, rows in by_symbol_rows.items()
        ],
        key=lambda x: x["n"],
        reverse=True,
    )

    # Par famille
    by_family_rows: dict[str, list[dict]] = defaultdict(list)
    for r in scored:
        by_family_rows[_symbol_family(r["symbol"])].append(r)

    by_family = sorted(
        [
            {
                "family": fam,
                "n": len(rows),
                "scorable": sum(1 for r in rows if r["verdict"] != "UNKNOWN"),
                "win_rate": _win_rate(rows),
            }
            for fam, rows in by_family_rows.items()
        ],
        key=lambda x: x["n"],
        reverse=True,
    )

    return {
        "total": total,
        "scorable": len(scorable),
        "win": by_verdict["WIN"],
        "loss": by_verdict["LOSS"],
        "neutral": by_verdict["NEUTRAL"],
        "unknown": by_verdict["UNKNOWN"],
        "win_rate_all_scorable": _win_rate(scored),
        "win_rate_executed": _win_rate(executed_rows),
        "win_rate_hold_only": _win_rate(hold_rows),
        "win_rate_buy_sell_only": _win_rate(buy_sell_rows),
        "top10_win_notes": top_win_notes,
        "top10_loss_notes": top_loss_notes,
        "by_symbol": by_symbol,
        "by_family": by_family,
    }


# ---------------------------------------------------------------------------
# Entrée principale
# ---------------------------------------------------------------------------


def main() -> int:  # noqa: C901
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="chemin du fichier JSON de sortie")
    parser.add_argument("--verbose", "-v", action="store_true", help="logs DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    out_path = Path(args.out)

    # 1. Charger les learnings
    learnings = load_learnings()
    log.info("Learnings après dédup : %d", len(learnings))

    # 2. Charger et indexer les décisions
    decisions_by_id = load_decisions()
    ts_sym_index = _build_ts_symbol_index(decisions_by_id)

    # 3. Associer chaque learning à sa décision
    matched = 0
    unmatched = 0
    enriched: list[tuple[dict, dict | None]] = []
    for learning in learnings:
        did = str(learning.get("decision_id") or "")
        dec: dict | None = None
        if did:
            dec = decisions_by_id.get(did)
        if dec is None:
            # Tentative de jointure sur (ts, symbol)
            ts = str(learning.get("ts") or "")
            sym = str(learning.get("symbol") or "")
            dec = ts_sym_index.get((ts, sym))
        if dec is not None:
            matched += 1
        else:
            unmatched += 1
        enriched.append((learning, dec))

    log.info("Learnings avec décision trouvée : %d / %d (non trouvés : %d)", matched, len(learnings), unmatched)

    # 4. Charger l'historique prix pour tous les symboles
    symbols = sorted({str(lrn.get("symbol") or "") for lrn, _ in enriched if lrn.get("symbol")})
    start, end = _date_range(learnings)
    log.info("Chargement historique 1h pour %d symboles (%s → %s)…", len(symbols), start, end)
    history, unavailable = load_history(symbols, start, end)
    if unavailable:
        log.warning("Symboles sans historique : %s", unavailable)

    # 5. Scorer chaque learning
    log.info("Scoring de %d learnings…", len(enriched))
    scored: list[dict] = []
    for learning, dec in enriched:
        row = score_learning(learning, dec, history)
        scored.append(row)

    # 6. Agrégats
    aggregates = compute_aggregates(scored)

    # 7. Construire le document de sortie
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "horizons": [lbl for lbl, _ in _HORIZONS],
            "primary_horizon": _PRIMARY_HORIZON,
            "band": _BAND,
            "judgeable_reasons": ["hold", "ok"],
            "note_trunc": NOTE_TRUNC,
            "sources": {
                "archive": str(LEARNINGS_ARCHIVE),
                "live": str(LEARNINGS_LIVE),
            },
        },
        "counts": {
            "learnings_loaded": len(learnings),
            "decisions_indexed": len(decisions_by_id),
            "matched_to_decision": matched,
            "unmatched": unmatched,
            "unavailable_symbols": len(unavailable),
            **{k: aggregates[k] for k in ("total", "scorable", "win", "loss", "neutral", "unknown")},
        },
        "unavailable_symbols": unavailable,
        "aggregates": {k: aggregates[k] for k in aggregates if k not in ("total", "scorable", "win", "loss", "neutral", "unknown")},
        "learnings": scored,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Rapport écrit : %s", out_path)

    # Résumé console
    c = report["counts"]
    agg = report["aggregates"]
    print("\n=== FLAIR bootstrap learnings-outcome ===")
    print(f"Learnings chargés  : {c['learnings_loaded']}")
    print(f"Matchés à décision : {c['matched_to_decision']} / {c['learnings_loaded']}")
    print(f"Scorables (non-UNKNOWN) : {c['scorable']} / {c['total']}")
    print(f"WIN  : {c['win']:>4}   LOSS  : {c['loss']:>4}   NEUTRAL : {c['neutral']:>4}   UNKNOWN : {c['unknown']:>4}")
    wr = agg["win_rate_all_scorable"]
    wr_ex = agg["win_rate_executed"]
    wr_hold = agg["win_rate_hold_only"]
    wr_bs = agg["win_rate_buy_sell_only"]
    print(f"Win rate global    : {wr:.1%}" if wr is not None else "Win rate global    : n/a")
    print(f"  Décisions exec   : {wr_ex:.1%}" if wr_ex is not None else "  Décisions exec   : n/a")
    print(f"  HOLD uniquement  : {wr_hold:.1%}" if wr_hold is not None else "  HOLD uniquement  : n/a")
    print(f"  BUY/SELL         : {wr_bs:.1%}" if wr_bs is not None else "  BUY/SELL         : n/a")
    if unavailable:
        print(f"Symboles indisponibles : {len(unavailable)} ({', '.join(unavailable[:5])}{'…' if len(unavailable) > 5 else ''})")
    print(f"\nFichier : {out_path}")

    wins = [r for r in scored if r["verdict"] == "WIN"]
    losses = [r for r in scored if r["verdict"] == "LOSS"]
    print("\n--- 3 exemples WIN ---")
    for r in wins[:3]:
        print(f"  [{r['symbol']} {r['action']}] {r['note'][:80]}…  (fwd1d={r['forward_return_1d']})")
    print("--- 3 exemples LOSS ---")
    for r in losses[:3]:
        print(f"  [{r['symbol']} {r['action']}] {r['note'][:80]}…  (fwd1d={r['forward_return_1d']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
