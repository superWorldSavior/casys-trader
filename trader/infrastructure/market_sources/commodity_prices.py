"""commodity_prices — collecte quotidienne du Brent et de l'Or via Yahoo Finance.

Remplace les séries IMF/PCPS (IMF/PCPS/M.W00.POILBRE.USD et IMF/PCPS/M.W00.PGOLD.USD)
mortes depuis 2025-07. Source : endpoint Yahoo Finance v8/finance/chart, tickers
BZ=F (Brent crude front-month) et GC=F (Gold front-month).

Pattern identique à macro_series : thread daemon fire-and-forget, marqueur
.last_collect propre (state/macro_series/commodity_prices.last_collect),
dédup par period, best-effort total (jamais d'impact sur le cycle).

Sortie JSONL : state/macro_series/brent_crude_usd.jsonl et gold_usd.jsonl —
même format que macro_series ({ts_collected, series_id, period, value}).

CAVEAT front-month (roulement des contrats futures) :
  BZ=F et GC=F sont des contrats futures front-month (premier contrat actif).
  Yahoo Finance bascule automatiquement vers le contrat suivant ~le 20 du mois
  (quelques jours avant expiration). Ce jour-là, le close peut faire un saut
  de quelques USD (l'écart entre les deux contrats) qui ne reflète PAS un
  mouvement du marché physique. Ce bruit de roulement est inhérent aux données
  futures continus et est documenté ici pour que l'analyste en soit conscient.
  Les données sont stockées comme reçues de Yahoo, sans correction de roulement.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COLLECT_COOLDOWN_H = 20
MARKER_FILE = "commodity_prices.last_collect"
DEFAULT_TIMEOUT_S = 8

_V8_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
_UA = "Mozilla/5.0 (compatible; casys-trader/1.0)"

# Tickers Yahoo Finance front-month → label JSONL de sortie
COMMODITIES: tuple[dict, ...] = (
    {
        "ticker": "BZ=F",
        "series_id": "yahoo/BZ=F",
        "label": "brent_crude_usd",
    },
    {
        "ticker": "GC=F",
        "series_id": "yahoo/GC=F",
        "label": "gold_usd",
    },
)

# ---------------------------------------------------------------------------
# Verrou module — empêche un second thread si la collecte est en cours
# ---------------------------------------------------------------------------

_collect_lock = threading.Lock()
_collect_in_progress: bool = False

# ---------------------------------------------------------------------------
# Transport HTTP (ré-utilise le pattern de macro_series, adapté Yahoo)
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> str:
    """GET brut → texte. Lève urllib.error.URLError en cas d'erreur."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:  # noqa: S310
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise urllib.error.URLError(f"HTTP {exc.code}: {body[:200]}") from exc


# ---------------------------------------------------------------------------
# Extraction du close quotidien depuis la réponse Yahoo v8
# ---------------------------------------------------------------------------


def _fetch_last_close(
    ticker: str,
    *,
    http_get: Callable[[str], str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[str, float] | None:
    """Récupère le close de la dernière barre quotidienne via Yahoo v8.

    Retourne (period: 'YYYY-MM-DD', value: float) ou None si indisponible.
    Jamais d'exception — best-effort.

    Les données sont issues d'un contrat futures front-month : voir le caveat
    de roulement documenté en tête de module.
    """
    _get = http_get if http_get is not None else lambda url: _http_get(url, timeout_s=timeout_s)
    try:
        body = _get(f"{_V8_CHART}{ticker}?range=5d&interval=1d")
        chart = json.loads(body)["chart"]

        error = chart.get("error")
        if error:
            log.warning("commodity_prices: chart.error pour %s : %s", ticker, error)
            return None

        results = chart.get("result") or []
        if not results:
            return None

        result = results[0]
        timestamps = result.get("timestamp") or []
        if not timestamps:
            return None

        indicators = result.get("indicators") or {}
        quote = ((indicators.get("quote") or [{}]) or [{}])[0]
        closes = quote.get("close") or []

        # Cherche le dernier close non-null en partant de la fin
        last_ts: int | None = None
        last_close: float | None = None
        for ts, raw_close in zip(reversed(timestamps), reversed(closes)):
            if raw_close is not None and math.isfinite(float(raw_close)) and float(raw_close) > 0:
                last_ts = int(ts)
                last_close = float(raw_close)
                break

        if last_ts is None or last_close is None:
            return None

        period = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return period, last_close

    except Exception as exc:  # noqa: BLE001 — best-effort, frontière externe
        log.warning("commodity_prices: erreur fetch %s : %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Dédup : réutilise le helper de macro_series (copie locale pour isolation)
# ---------------------------------------------------------------------------


def _last_collected_period(path: Path) -> str | None:
    """Lit la dernière ligne du JSONL et retourne son champ 'period' (ou None)."""
    if not path.exists():
        return None
    try:
        lines = [ln.strip() for ln in path.read_text("utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        return json.loads(lines[-1]).get("period")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Collecte principale
# ---------------------------------------------------------------------------


def collect_daily(
    state_dir: Path,
    now: datetime,
    *,
    http_get: Callable[[str], str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    commodities: tuple[dict, ...] = COMMODITIES,
) -> dict:
    """Collecte le dernier close quotidien de chaque commodité et l'append en JSONL.

    Pour chaque commodité :
      - Fetch GET Yahoo v8 → extrait (period: YYYY-MM-DD, close)
      - Si period nouvelle (vs tail du JSONL) → append {ts_collected, series_id, period, value}
      - Si period identique au dernier enregistrement → skip (dédup par period)
    Erreur par commodité : avalée + loggée, les autres continuent (best-effort).

    Retourne {"collected": int, "skipped": int, "errors": int}.

    Arguments :
      state_dir   — répertoire racine de l'état (ex : state/)
      now         — horodatage de collecte (datetime avec ou sans tzinfo)
      http_get    — transport injectable (fn(url) → str) ; None = urllib
      timeout_s   — timeout HTTP (ignoré si http_get fourni)
      commodities — liste de commodités à collecter (injectable pour les tests)
    """
    macro_dir = Path(state_dir) / "macro_series"
    macro_dir.mkdir(parents=True, exist_ok=True)

    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    ts_collected = now_utc.isoformat()
    collected = skipped = errors = 0

    for c in commodities:
        ticker = c["ticker"]
        series_id = c["series_id"]
        label = c["label"]
        jsonl_path = macro_dir / f"{label}.jsonl"
        try:
            obs = _fetch_last_close(ticker, http_get=http_get, timeout_s=timeout_s)
            if obs is None:
                log.warning("commodity_prices: aucun close pour %s", ticker)
                errors += 1
                continue
            period, value = obs
            last_period = _last_collected_period(jsonl_path)
            if last_period == period:
                log.debug("commodity_prices: skip %s period=%s (déjà collectée)", label, period)
                skipped += 1
                continue
            row = {
                "ts_collected": ts_collected,
                "series_id": series_id,
                "period": period,
                "value": value,
            }
            with jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            log.info("commodity_prices: +1 %s period=%s value=%s", label, period, value)
            collected += 1
        except Exception as exc:  # noqa: BLE001 — best-effort par commodité
            log.warning("commodity_prices: erreur sur %s : %s", ticker, exc)
            errors += 1

    return {"collected": collected, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Marqueur de dernière collecte
# ---------------------------------------------------------------------------


def _read_last_collect(marker: Path) -> datetime | None:
    """Lit la date de dernière collecte depuis le fichier marqueur."""
    try:
        raw = marker.read_text("utf-8").strip()
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return ts.astimezone(timezone.utc)
    except (OSError, ValueError):
        return None


def _write_last_collect(marker: Path, now_utc: datetime) -> None:
    """Écrit la date de collecte dans le marqueur (best-effort)."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now_utc.isoformat(), encoding="utf-8")
    except OSError as exc:
        log.warning("commodity_prices: écriture marqueur échouée : %s", exc)


# ---------------------------------------------------------------------------
# Déclenchement best-effort — thread daemon fire-and-forget
# ---------------------------------------------------------------------------


def maybe_collect(
    state_dir: Path,
    now: datetime,
    *,
    http_get: Callable[[str], str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cooldown_h: float = COLLECT_COOLDOWN_H,
) -> dict:
    """Déclenche collect_daily dans un thread daemon fire-and-forget si cooldown dépassé.

    Retourne immédiatement — le cycle n'est jamais bloqué.

    Le marqueur commodity_prices.last_collect est posé AU LANCEMENT du thread
    pour bloquer un double départ si le cycle suivant arrive pendant la collecte.
    Un verrou module (_collect_lock) garantit qu'un seul thread tourne à la fois.

    Retourne :
      {"triggered": False, "reason": "cooldown",     "elapsed_h": float}
      {"triggered": False, "reason": "in_progress"}
      {"triggered": True,  "_thread": threading.Thread}
    """
    global _collect_in_progress

    macro_dir = Path(state_dir) / "macro_series"
    marker = macro_dir / MARKER_FILE
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)

    last = _read_last_collect(marker)
    if last is not None:
        elapsed_h = (now_utc - last).total_seconds() / 3600.0
        if elapsed_h < cooldown_h:
            return {
                "triggered": False,
                "reason": "cooldown",
                "elapsed_h": round(elapsed_h, 2),
            }

    with _collect_lock:
        if _collect_in_progress:
            return {"triggered": False, "reason": "in_progress"}
        _collect_in_progress = True

    # Le marqueur est posé APRÈS une collecte réussie (dans _run) et non avant le
    # lancement du thread. Ainsi un échec Yahoo ne bloque plus le retry ~20 h.

    def _run() -> None:
        global _collect_in_progress
        try:
            result = collect_daily(state_dir, now, http_get=http_get, timeout_s=timeout_s)
            # Écrire le marqueur uniquement si au moins une série a été traitée
            # sans exception (collected ou skipped > 0).
            if result.get("collected", 0) + result.get("skipped", 0) > 0:
                _write_last_collect(marker, now_utc)
        except Exception as exc:  # noqa: BLE001 — best-effort total
            log.warning("commodity_prices: échec inattendu collect_daily : %s", exc)
            # Pas de marqueur en cas d'exception → retry au cycle suivant
        finally:
            with _collect_lock:
                _collect_in_progress = False

    t = threading.Thread(target=_run, daemon=True, name="commodity-prices-collect")
    t.start()
    return {"triggered": True, "_thread": t}


__all__ = ["collect_daily", "maybe_collect", "COMMODITIES", "_fetch_last_close"]
