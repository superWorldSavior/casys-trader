"""Probe de couverture earnings Yahoo — univers casys-trader.

Pour chaque symbole de config/universe.yaml :
  - méthode prod : get_earnings_dates(limit=12)
  - méthode alt   : tk.calendar (Earnings Date)
Compare la couverture par zone (US / EU / TW) et diagnostique pourquoi
les buckets <168h sont quasi-vides en prod (23/06–02/07).

Usage :
  uv run python scripts/earnings_coverage_probe.py
  uv run python scripts/earnings_coverage_probe.py --out state/archive/earnings-coverage-2026-07-02.json
  uv run ruff check scripts/earnings_coverage_probe.py
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "state" / "archive" / "earnings-coverage-2026-07-02.json"
UNIVERSE_YAML = ROOT / "config" / "universe.yaml"

# Fenêtre de référence : maintenant en UTC (date du rapport)
_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

# Seuils buckets (miroir de news_attribution_measure.py)
_BUCKET_THRESHOLDS = [24, 72, 168]  # heures


def _classify_zone(symbol: str) -> str:
    if symbol.endswith(".TW"):
        return "TW"
    suffix_lower = symbol.split(".")[-1].lower() if "." in symbol else ""
    eu_suffixes = {
        "pa", "de", "as", "mi", "sw", "mc", "l", "vi", "co", "he", "st", "br",
    }
    if suffix_lower in eu_suffixes:
        return "EU"
    return "US"


def _earnings_bucket(hours: float | None) -> str:
    if hours is None:
        return "null"
    if hours < 24:
        return "<24h"
    if hours < 72:
        return "24-72h"
    if hours < 168:
        return "72-168h"
    return ">168h"


def _hours_to(now: datetime, dt: datetime) -> float:
    return round((dt - now).total_seconds() / 3600.0, 1)


def _next_future(now: datetime, dates: list[datetime]) -> datetime | None:
    future = [d for d in dates if d > now]
    return min(future) if future else None


def _probe_symbol(symbol: str, now: datetime) -> dict:
    """Interroge Yahoo pour un symbole. Retourne un dict de résultats."""
    import yfinance as yf  # noqa: PLC0415

    result: dict = {
        "symbol": symbol,
        "zone": _classify_zone(symbol),
        "mapped": None,
        # méthode prod : get_earnings_dates
        "prod_dates_count": 0,
        "prod_dates_sample": [],
        "prod_next_future": None,
        "prod_next_future_h": None,
        "prod_bucket": "null",
        "prod_has_future": False,
        # méthode alt : calendar
        "cal_earnings_date": None,
        "cal_next_future": None,
        "cal_next_future_h": None,
        "cal_bucket": "null",
        "cal_has_future": False,
        # erreurs
        "errors": [],
    }

    tk = yf.Ticker(symbol)

    # ── mapped ──────────────────────────────────────────────────────────────
    try:
        last = tk.fast_info.get("lastPrice") or tk.fast_info.get("last_price")
        result["mapped"] = last is not None
    except Exception as exc:
        result["errors"].append(f"fast_info: {exc}")
        result["mapped"] = False

    # ── méthode prod : get_earnings_dates(limit=12) ─────────────────────────
    try:
        df = tk.get_earnings_dates(limit=12)
        if df is not None and getattr(df, "index", None) is not None and len(df) > 0:
            dates_prod: list[datetime] = []
            for ts in df.index:
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                dates_prod.append(dt)
            result["prod_dates_count"] = len(dates_prod)
            # 3 plus récentes (pour debug)
            result["prod_dates_sample"] = [d.isoformat() for d in sorted(dates_prod)[-3:]]
            next_dt = _next_future(now, dates_prod)
            if next_dt is not None:
                result["prod_next_future"] = next_dt.isoformat()
                h = _hours_to(now, next_dt)
                result["prod_next_future_h"] = h
                result["prod_bucket"] = _earnings_bucket(h)
                result["prod_has_future"] = True
    except Exception as exc:
        result["errors"].append(f"get_earnings_dates: {exc}")

    # ── méthode alt : calendar ───────────────────────────────────────────────
    try:
        cal = tk.calendar
        # yfinance 1.4 : tk.calendar est un dict ou un DataFrame selon la version
        cal_dt: datetime | None = None
        if isinstance(cal, dict):
            # clés possibles : "Earnings Date", "earningsDate"
            for key in ("Earnings Date", "earningsDate"):
                val = cal.get(key)
                if val is not None:
                    # peut être une liste ou une valeur directe
                    if isinstance(val, list):
                        val = val[0] if val else None
                    if val is not None:
                        if hasattr(val, "to_pydatetime"):
                            val = val.to_pydatetime()
                        if isinstance(val, datetime):
                            if val.tzinfo is None:
                                val = val.replace(tzinfo=timezone.utc)
                            cal_dt = val.astimezone(timezone.utc)
                        elif hasattr(val, "isoformat"):
                            # date sans timezone
                            from datetime import date  # noqa: PLC0415

                            if isinstance(val, date):
                                cal_dt = datetime(
                                    val.year, val.month, val.day, tzinfo=timezone.utc
                                )
                    if cal_dt:
                        break
        elif hasattr(cal, "loc"):
            # DataFrame (ancienne API)
            for key in ("Earnings Date", "earningsDate"):
                try:
                    val = cal.loc[key].iloc[0] if key in cal.index else None
                    if val is not None and hasattr(val, "to_pydatetime"):
                        d = val.to_pydatetime()
                        if d.tzinfo is None:
                            d = d.replace(tzinfo=timezone.utc)
                        cal_dt = d.astimezone(timezone.utc)
                        break
                except Exception:
                    pass

        if cal_dt:
            result["cal_earnings_date"] = cal_dt.isoformat()
            if cal_dt > now:
                result["cal_next_future"] = cal_dt.isoformat()
                h = _hours_to(now, cal_dt)
                result["cal_next_future_h"] = h
                result["cal_bucket"] = _earnings_bucket(h)
                result["cal_has_future"] = True
    except Exception as exc:
        result["errors"].append(f"calendar: {exc}")

    return result


def _zone_stats(records: list[dict], method: str) -> dict:
    """Statistiques de couverture pour une zone et une méthode (prod / cal)."""
    n = len(records)
    if n == 0:
        return {"n": 0}
    has_future_key = f"{method}_has_future"
    bucket_key = f"{method}_bucket"
    covered = sum(1 for r in records if r.get(has_future_key))
    bucket_counts: dict[str, int] = {}
    for r in records:
        bkt = r.get(bucket_key, "null")
        bucket_counts[bkt] = bucket_counts.get(bkt, 0) + 1
    return {
        "n": n,
        "covered": covered,
        "coverage_pct": round(covered / n * 100, 1),
        "null_count": bucket_counts.get("null", 0),
        "buckets": bucket_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Fichier JSON de sortie",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # ── Chargement univers ───────────────────────────────────────────────────
    try:
        import yaml  # noqa: PLC0415

        with UNIVERSE_YAML.open() as fh:
            universe_data = yaml.safe_load(fh)
        symbols: list[str] = universe_data.get("symbols", [])
    except ImportError:
        # fallback sans pyyaml
        symbols = []
        for line in UNIVERSE_YAML.read_text().splitlines():
            line = line.strip()
            if line.startswith("- "):
                symbols.append(line[2:].strip())

    log.info("Univers chargé : %d symboles", len(symbols))
    now = _NOW
    log.info("Référence now : %s", now.isoformat())

    # ── Probe par symbole ────────────────────────────────────────────────────
    records: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        log.info("[%d/%d] %s …", i, len(symbols), sym)
        rec = _probe_symbol(sym, now)
        records.append(rec)
        if rec["errors"]:
            log.warning("  Erreurs %s : %s", sym, rec["errors"])
        log.info(
            "  prod_bucket=%-10s cal_bucket=%-10s prod_h=%s cal_h=%s",
            rec["prod_bucket"],
            rec["cal_bucket"],
            rec.get("prod_next_future_h"),
            rec.get("cal_next_future_h"),
        )
        time.sleep(0.3)  # rate-limit Yahoo

    # ── Agrégats par zone ────────────────────────────────────────────────────
    zones = ["US", "EU", "TW"]
    by_zone_prod: dict[str, dict] = {}
    by_zone_cal: dict[str, dict] = {}
    for zone in zones:
        subset = [r for r in records if r["zone"] == zone]
        by_zone_prod[zone] = _zone_stats(subset, "prod")
        by_zone_cal[zone] = _zone_stats(subset, "cal")

    # ── Diagnostic inter-saison ──────────────────────────────────────────────
    # Pour chaque symbole avec données futures, est-ce >168h depuis le 23/06 ?
    ref_start = datetime(2026, 6, 23, 0, 0, 0, tzinfo=timezone.utc)
    # Si next_earnings est dans le futur depuis now (02/07),
    # il était forcément >168h depuis le 23/06 s'il est à plus de 216h de now.
    # (168h + 9 jours de fenêtre = 168 + 216 = 384h depuis la fin de fenêtre)
    inter_season_count = 0
    for r in records:
        if r["prod_has_future"]:
            h_from_start = _hours_to(ref_start, datetime.fromisoformat(r["prod_next_future"]))
            if h_from_start > 168:
                inter_season_count += 1

    total = len(records)
    prod_covered = sum(1 for r in records if r["prod_has_future"])
    cal_covered = sum(1 for r in records if r["cal_has_future"])

    # ── Rapport console ──────────────────────────────────────────────────────
    print("\n=== EARNINGS COVERAGE PROBE — 2026-07-02 ===")
    print(f"Univers : {total} symboles | now = {now.isoformat()}")
    print()
    print("── Couverture prod (get_earnings_dates) ──")
    print(f"  Symboles avec date future : {prod_covered}/{total} ({prod_covered/total*100:.0f}%)")
    for zone in zones:
        s = by_zone_prod[zone]
        print(
            f"  {zone:<3}: {s['covered']}/{s['n']} couverts "
            f"({s['coverage_pct']:.0f}%)  null={s['null_count']}  "
            f"buckets={s['buckets']}"
        )
    print()
    print("── Couverture alt (calendar) ──")
    print(f"  Symboles avec date future : {cal_covered}/{total} ({cal_covered/total*100:.0f}%)")
    for zone in zones:
        s = by_zone_cal[zone]
        print(
            f"  {zone:<3}: {s['covered']}/{s['n']} couverts "
            f"({s['coverage_pct']:.0f}%)  null={s['null_count']}  "
            f"buckets={s['buckets']}"
        )
    print()
    print("── Détail par symbole (prod) ──")
    for r in records:
        flag = "✓" if r["prod_has_future"] else "✗"
        print(
            f"  {flag} {r['symbol']:<16} zone={r['zone']}  "
            f"bucket={r['prod_bucket']:<12}  "
            f"h={str(r.get('prod_next_future_h', 'n/a')):<8}  "
            f"cal_bucket={r['cal_bucket']}"
        )
    print()
    print(
        f"Symboles avec next earnings >168h depuis le 23/06 : "
        f"{inter_season_count}/{prod_covered}"
    )

    # ── Écriture JSON ────────────────────────────────────────────────────────
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "probe_ts": now.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": total,
        "summary": {
            "prod_covered": prod_covered,
            "prod_covered_pct": round(prod_covered / total * 100, 1),
            "cal_covered": cal_covered,
            "cal_covered_pct": round(cal_covered / total * 100, 1),
            "inter_season_all_gt168h_from_2306": inter_season_count,
        },
        "by_zone_prod": by_zone_prod,
        "by_zone_cal": by_zone_cal,
        "records": records,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Rapport écrit : %s", out_path)
    print(f"\nRapport JSON : {out_path}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
