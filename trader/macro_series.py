"""macro_series — collecte quotidienne de séries macro via DBnomics (P1a, spec §7).

Zéro dépendance nouvelle : GET via urllib, analogue à _post_json dans trader/llm.py.
Déclenché best-effort en fin de cycle daemon si dernière collecte > 20 h
(marqueur state/macro_series/.last_collect). Toute exception est avalée —
jamais d'impact sur le cycle.

Séries versionnées (v1) — identifiants vérifiés DBnomics 2026-07-02 :
  FED/H15/RIFSPFF_N.D            Fed funds effectif (quotidien)
  BLS/cu/CUSR0000SA0             CPI US tous postes (mensuel)
  ECB/FM/B.U2.EUR.4F.KR.DFR.LEV Taux de dépôt BCE (journalier)
  Eurostat/prc_hicp_midx/M.I15.CP00.EA20  HICP zone euro (mensuel)
  BLS/ln/LNS14000000             Taux de chômage US (mensuel)

Note ECB : DBnomics/ECB peut avoir un décalage de quelques jours vs la BCE
(publication officielle → agrégation DBnomics). L'important est le mécanisme :
même pattern pour tout fournisseur accessible sans clé API.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

DBNOMICS_BASE = "https://api.db.nomics.world/v22"
DEFAULT_TIMEOUT_S = 15
COLLECT_COOLDOWN_H = 20
MARKER_FILE = ".last_collect"

# ---------------------------------------------------------------------------
# Séries macro versionnées — v1
# Champ `id`    = "PROVIDER/DATASET/SERIES" (identifiant DBnomics complet).
# Champ `label` = nom court → nom du fichier JSONL (state/macro_series/<label>.jsonl).
# ---------------------------------------------------------------------------
SERIES: tuple[dict, ...] = (
    {
        "id": "FED/H15/RIFSPFF_N.D",
        "label": "fed_funds_effective",
    },
    {
        "id": "BLS/cu/CUSR0000SA0",
        "label": "cpi_us_all_items",
    },
    {
        "id": "ECB/FM/B.U2.EUR.4F.KR.DFR.LEV",
        "label": "ecb_deposit_rate",
    },
    {
        "id": "Eurostat/prc_hicp_midx/M.I15.CP00.EA20",
        "label": "hicp_euro_area",
    },
    {
        "id": "BLS/ln/LNS14000000",
        "label": "unemployment_rate_us",
    },
)


# ---------------------------------------------------------------------------
# Client HTTP minimal (pattern _post_json de trader/llm.py)
# ---------------------------------------------------------------------------


def _get_json(url: str, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """GET → JSON dict. Lève urllib.error.URLError en cas d'erreur HTTP ou réseau."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise urllib.error.URLError(f"HTTP {exc.code}: {body[:200]}") from exc


def _series_url(series_id: str) -> str:
    """Construit l'URL DBnomics pour une série (dernière observation uniquement)."""
    return f"{DBNOMICS_BASE}/series/{series_id}?observations=1"


def _extract_last_observation(data: dict) -> tuple[str, float] | None:
    """Extrait (period, value) de la dernière observation dans la réponse DBnomics.

    Retourne None si la structure est absente, vide ou non parsable.
    Jamais d'exception — best-effort.
    """
    try:
        docs = data["series"]["docs"]
        if not docs:
            return None
        doc = docs[0]
        periods = doc.get("period", [])
        values = doc.get("value", [])
        if not periods or not values:
            return None
        period = str(periods[-1])
        raw_val = values[-1]
        if raw_val is None:
            return None
        return period, float(raw_val)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dédup : lire le dernier period collecté pour une série
# ---------------------------------------------------------------------------


def _last_collected_period(path: Path) -> str | None:
    """Lit la dernière ligne du JSONL et retourne son champ 'period' (ou None).

    Jamais d'exception — best-effort.
    """
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
    get_json: Callable[[str], dict] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    series: tuple[dict, ...] = SERIES,
) -> dict:
    """Collecte la dernière observation de chaque série macro et l'append en JSONL.

    Pour chaque série :
      - Fetch GET DBnomics → extrait (period, value)
      - Si period nouvelle (vs tail du JSONL) → append {ts_collected, series_id, period, value}
      - Si period identique au dernier enregistrement → skip (dédup par period)
    Erreur par série : avalée + loggée, les autres continuent (best-effort).

    Retourne {"collected": int, "skipped": int, "errors": int}.

    Arguments :
      state_dir   — répertoire racine de l'état (ex : state/)
      now         — horodatage de collecte (datetime avec ou sans tzinfo)
      get_json    — injectable pour les tests (fn(url) → dict) ; None = _get_json
      timeout_s   — timeout HTTP (ignoré si get_json fourni)
      series      — liste de séries à collecter (injectable pour les tests)
    """
    _fetch = get_json if get_json is not None else lambda url: _get_json(url, timeout_s=timeout_s)

    macro_dir = Path(state_dir) / "macro_series"
    macro_dir.mkdir(parents=True, exist_ok=True)

    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    ts_collected = now_utc.isoformat()
    collected = skipped = errors = 0

    for s in series:
        sid = s["id"]
        label = s.get("label", sid.replace("/", "_"))
        jsonl_path = macro_dir / f"{label}.jsonl"
        try:
            url = _series_url(sid)
            data = _fetch(url)
            obs = _extract_last_observation(data)
            if obs is None:
                log.warning("macro_series: aucune observation pour %s", sid)
                errors += 1
                continue
            period, value = obs
            last_period = _last_collected_period(jsonl_path)
            if last_period == period:
                log.debug("macro_series: skip %s period=%s (déjà collectée)", label, period)
                skipped += 1
                continue
            row = {
                "ts_collected": ts_collected,
                "series_id": sid,
                "period": period,
                "value": value,
            }
            with jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            log.info("macro_series: +1 %s period=%s value=%s", label, period, value)
            collected += 1
        except Exception as exc:  # noqa: BLE001 — best-effort par série, jamais de remontée
            log.warning("macro_series: erreur sur %s : %s", sid, exc)
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
        log.warning("macro_series: écriture marqueur échouée : %s", exc)


# ---------------------------------------------------------------------------
# Déclenchement best-effort — pattern consolidateur
# ---------------------------------------------------------------------------


def maybe_collect(
    state_dir: Path,
    now: datetime,
    *,
    get_json: Callable[[str], dict] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cooldown_h: float = COLLECT_COOLDOWN_H,
) -> dict:
    """Déclenche collect_daily si dernière collecte > cooldown_h heures (défaut 20 h).

    Best-effort total : toute exception est avalée, jamais d'impact sur le cycle daemon.

    Retourne :
      {"triggered": False, "reason": "cooldown", "elapsed_h": float}
        → si la collecte est trop récente
      {"triggered": True, "collected": int, "skipped": int, "errors": int}
        → si la collecte s'est déroulée (même en erreur partielle ou totale)
    """
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

    try:
        result = collect_daily(state_dir, now, get_json=get_json, timeout_s=timeout_s)
        _write_last_collect(marker, now_utc)
        return {"triggered": True, **result}
    except Exception as exc:  # noqa: BLE001 — best-effort total, jamais de remontée
        log.warning("macro_series: échec inattendu collect_daily : %s", exc)
        # On écrit quand même le marqueur pour éviter une boucle de retry frénétique
        _write_last_collect(marker, now_utc)
        return {"triggered": True, "collected": 0, "skipped": 0, "errors": 1, "error": str(exc)}
