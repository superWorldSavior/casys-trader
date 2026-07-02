"""Runtime read model assembly for the casys-trader UI."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trader.indicator_watch import is_armed_plan as _is_armed_plan

# Racine du repo (trois niveaux au-dessus de ce fichier)
_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_DIR = _ROOT / "state"
_CURRENT_REPORT_FILE = _STATE_DIR / "current_report.json"
_LAST_REPORT_FILE = _STATE_DIR / "last_report.json"
_STATUS_FILE = _STATE_DIR / "daemon_status.json"
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Lecture d'état
# ---------------------------------------------------------------------------


def load_state(path: str | Path) -> dict | None:
    """Lit le fichier JSON et retourne le dict, ou None si absent/illisible.

    Ne lève jamais d'exception — conçu pour une boucle de polling.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    """Convertit en float fini, sinon retourne ``default``."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_list_of_dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _format_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _load_equity_curve(history_path: Path) -> list[float]:
    """Lit les points d'équité non nuls depuis history.jsonl, sans lever."""
    if not history_path.exists():
        return []
    values: list[float] = []
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or row.get("equity") is None:
            continue
        equity = _safe_float(row.get("equity"), default=None)
        if equity is not None:
            values.append(equity)
    return values


def _compute_live_kpis_safe(state_dir: Path) -> dict:
    try:
        from trader.reporting.stats import compute_live_kpis

        result = compute_live_kpis(state_dir)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _read_min_trade_confidence_safe() -> float:
    """Lit min_trade_confidence depuis config/risk.yaml. Fail-safe → 0.7."""
    try:
        raw = yaml.safe_load((_ROOT / "config" / "risk.yaml").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            value = float(raw.get("min_trade_confidence", 0.7))
            if 0.0 <= value <= 1.0:
                return value
    except Exception:
        pass
    return 0.7


def _compute_attribution_safe(state_dir: Path) -> dict:
    try:
        from trader.reporting.attribution import compute_attribution

        result = compute_attribution(
            state_dir,
            min_entry_confidence=_read_min_trade_confidence_safe(),
        )
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _load_learnings_safe(state_dir: Path, *, limit: int = 5) -> list[dict]:
    try:
        from trader.tools.memory import LearningsStore

        return _safe_list_of_dicts(
            LearningsStore(state_dir / "learnings.jsonl").recent(limit=limit)
        )
    except Exception:
        return []


def _load_trade_plans_safe(plans_path: Path) -> list[dict]:
    """Lit state/trade_plans.json, retourne la liste des plans ouverts.

    Tolérant : retourne [] si fichier absent, corrompu ou sans clé 'plans'.
    """
    try:
        raw = json.loads(plans_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return []
        plans = raw.get("plans", [])
        if not isinstance(plans, list):
            return []
        return [p for p in plans if isinstance(p, dict)]
    except Exception:
        return []


def _watch_is_expired(watch: dict, now: datetime) -> bool:
    """Vrai si la veille a une échéance passée. Sans échéance parsable : visible."""
    raw = watch.get("expires_at")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def _load_scheduler_data_safe(
    scheduler_path: Path, *, now: datetime | None = None
) -> tuple[list[dict], dict]:
    """Lit scheduler.json, retourne (indicator_watches, stale_streaks).

    indicator_watches : liste de dicts (valeurs du dict indicator_watches),
                        veilles expirées exclues (cohérent avec le daemon).
    stale_streaks     : dict {symbol: int}.
    Tolérant : retourne ([], {}) si absent/corrompu.
    """
    now = now or datetime.now(UTC)
    try:
        raw = json.loads(scheduler_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return [], {}
        watches_raw = raw.get("indicator_watches") or {}
        if isinstance(watches_raw, dict):
            candidates = [v for v in watches_raw.values() if isinstance(v, dict)]
        elif isinstance(watches_raw, list):
            candidates = [v for v in watches_raw if isinstance(v, dict)]
        else:
            candidates = []
        watches = [w for w in candidates if not _watch_is_expired(w, now)]
        streaks = raw.get("stale_streaks") or {}
        if not isinstance(streaks, dict):
            streaks = {}
        return watches, streaks
    except Exception:
        return [], {}


def _load_indicator_watches_safe(scheduler_path: Path) -> list[dict]:
    """Raccourci : ne retourne que les watches."""
    watches, _ = _load_scheduler_data_safe(scheduler_path)
    return watches


def _tail_decisions_safe(decisions_path: Path, *, n: int = 50) -> list[dict]:
    """Lit les n dernières lignes de decisions.jsonl sans tout charger.

    Algorithme tail : lit par blocs de 8192 octets depuis la fin, s'arrête
    quand n lignes valides collectées. Jamais d'exception.
    """
    try:
        if not decisions_path.exists():
            return []
        size = decisions_path.stat().st_size
        if size == 0:
            return []
        chunk_size = 8192
        collected: list[str] = []
        with decisions_path.open("rb") as fh:
            pos = size
            remainder = b""
            while pos > 0 and len(collected) < n:
                read_size = min(chunk_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size) + remainder
                lines_raw = chunk.split(b"\n")
                remainder = lines_raw[0]
                for line_bytes in reversed(lines_raw[1:]):
                    stripped = line_bytes.strip()
                    if stripped:
                        collected.append(stripped.decode("utf-8", errors="replace"))
                        if len(collected) >= n:
                            break
            # Dernier remainder
            if len(collected) < n and remainder.strip():
                collected.append(remainder.strip().decode("utf-8", errors="replace"))
        # collected est en ordre inversé
        result: list[dict] = []
        for raw_line in reversed(collected[:n]):
            try:
                obj = json.loads(raw_line)
                if isinstance(obj, dict):
                    result.append(obj)
            except Exception:
                continue
        return result
    except Exception:
        return []


def _enrich_decisions_with_data_source(
    decisions: list[dict], recent_decisions: list[dict]
) -> list[dict]:
    """Injecte 'data_source' dans chaque décision du rapport depuis les décisions récentes.

    Pour chaque décision du rapport, cherche la dernière entrée dans
    recent_decisions ayant le même symbole et injecte runtime.data_source.
    Retourne toujours une nouvelle liste (pas de mutation).
    """
    # Index : symbole → data_source le plus récent (dernier dans la liste = le plus récent)
    ds_index: dict[str, str | None] = {}
    for dec in recent_decisions:
        sym = str(dec.get("symbol") or "")
        if not sym:
            continue
        runtime = dec.get("runtime") if isinstance(dec.get("runtime"), dict) else {}
        ds = runtime.get("data_source") if isinstance(runtime, dict) else None
        ds_index[sym] = str(ds) if ds is not None else None

    enriched: list[dict] = []
    for dec in decisions:
        sym = str(dec.get("symbol") or "")
        copy = {**dec}
        if "data_source" not in copy:
            copy["data_source"] = ds_index.get(sym)
        enriched.append(copy)
    return enriched


def _load_consolidation_status_safe(status_path: Path) -> dict | None:
    """Lit learnings_consolidation_status.json. Retourne None si absent/corrompu."""
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _load_fills_safe(broker_path: Path, *, limit: int = 100) -> list[dict]:
    """Lit les fills depuis state/broker.json. Retourne [] si absent/corrompu."""
    try:
        raw = json.loads(broker_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return []
        fills = raw.get("fills", [])
        if not isinstance(fills, list):
            return []
        return _safe_list_of_dicts(fills[-limit:])
    except Exception:
        return []


def _load_venue_open_state_safe(
    state_dir: Path, config_dir: str
) -> tuple[dict, list[str]]:
    """Charge venue_state et open_venues. Retourne ({}, []) si indisponible."""
    try:
        from trader.rotation.venues import load_venue_state as _lvs
        from trader.rotation.schedule import load_sessions as _ls, open_venues as _ov

        venue_state = _lvs(state_dir)
        sessions = _ls(config_dir)
        now_iso = datetime.now(UTC).isoformat()
        ov_list = _ov(now_iso, sessions)
        return venue_state, ov_list
    except Exception:
        return {}, []


def _count_pending_learnings_safe(
    learnings_path: Path, consolidated_path: Path
) -> int:
    """Compte les learnings bruts NON consolidés (postérieurs au watermark).

    `learnings.jsonl` est un buffer rolling plafonné (DEFAULT_RAW_MAX_ENTRIES) :
    en compter toutes les lignes renvoie le cap (ex. 200), pas le vrai backlog.
    On ne compte que les entrées postérieures au watermark du store consolidé —
    le seul « pending » qui a du sens (ce qui reste à consolider).
    """
    try:
        if not learnings_path.exists():
            return 0
        watermark = None
        if consolidated_path.exists():
            payload = json.loads(consolidated_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                watermark = payload.get("watermark")
        raw_rows: list[dict] = []
        with learnings_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw_rows.append(json.loads(line))
                except Exception:
                    continue
        from trader.consolidator import select_new_raw

        return len(select_new_raw(raw_rows, watermark))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Cache module-level pour les noms de sociétés
# ---------------------------------------------------------------------------
_COMPANY_NAMES_CACHE: dict[str, str] = {}
_COMPANY_NAMES_PATH: str = ""


def _load_company_names(config_dir: str | Path) -> dict[str, str]:
    """Lit config/symbol_names.yaml et retourne un dict ticker→nom.

    Cache module-level rechargé uniquement si le chemin change (support tests).
    Retourne {} si fichier absent ou illisible — jamais d'exception.
    """
    global _COMPANY_NAMES_CACHE, _COMPANY_NAMES_PATH

    path = str(Path(config_dir) / "config" / "symbol_names.yaml")
    if path == _COMPANY_NAMES_PATH and _COMPANY_NAMES_CACHE:
        return _COMPANY_NAMES_CACHE

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _COMPANY_NAMES_CACHE = {str(k): str(v) for k, v in raw.items()}
            _COMPANY_NAMES_PATH = path
            return _COMPANY_NAMES_CACHE
    except Exception:
        pass

    # Fichier absent ou illisible → cache vide, chemin mémorisé pour éviter
    # les tentatives répétées sur le même chemin inexistant
    _COMPANY_NAMES_CACHE = {}
    _COMPANY_NAMES_PATH = path
    return {}


def _load_universe_symbols_safe(config_dir: str) -> list[str]:
    """Lit config/universe.yaml → data["symbols"]. Retourne [] si absent/illisible."""
    try:
        path = Path(config_dir) / "config" / "universe.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            symbols = raw.get("symbols")
            if isinstance(symbols, list):
                return [str(s) for s in symbols]
    except Exception:
        pass
    return []


def _load_starting_cash_safe(config_dir: str | Path) -> float | None:
    """Lit le capital de départ affiché par la barre cockpit."""
    try:
        from trader.config.portfolio import load_starting_cash

        return load_starting_cash(Path(config_dir) / "config")
    except Exception:
        return None


def load_runtime_state(
    *,
    state_dir: str | Path = _STATE_DIR,
    current_report_path: str | Path | None = None,
    last_report_path: str | Path | None = None,
    status_path: str | Path | None = None,
    config_dir: str | None = None,
) -> dict:
    """Charge le meilleur état affichable sans lever d'exception."""
    state_dir_path = Path(state_dir)
    current_report_path = (
        Path(current_report_path)
        if current_report_path is not None
        else state_dir_path / "current_report.json"
    )
    last_report_path = (
        Path(last_report_path)
        if last_report_path is not None
        else state_dir_path / "last_report.json"
    )
    status_path = (
        Path(status_path)
        if status_path is not None
        else state_dir_path / "daemon_status.json"
    )

    source = "none"
    raw = load_state(current_report_path)
    if isinstance(raw, dict):
        source = "current_report"
    else:
        raw = load_state(last_report_path)
        if isinstance(raw, dict):
            source = "last_report"
        else:
            raw = {}

    status = load_state(status_path)
    kpis = _compute_live_kpis_safe(state_dir_path)
    attribution = _compute_attribution_safe(state_dir_path)
    equity_curve = _load_equity_curve(state_dir_path / "history.jsonl")
    learnings = _load_learnings_safe(state_dir_path)
    trade_plans = _load_trade_plans_safe(state_dir_path / "trade_plans.json")
    indicator_watches, stale_streaks = _load_scheduler_data_safe(
        state_dir_path / "scheduler.json"
    )
    recent_decisions = _tail_decisions_safe(
        state_dir_path / "decisions.jsonl", n=50
    )
    consolidation_status = _load_consolidation_status_safe(
        state_dir_path / "learnings_consolidation_status.json"
    )
    learnings_pending_count = _count_pending_learnings_safe(
        state_dir_path / "learnings.jsonl",
        state_dir_path / "learnings_consolidated.json",
    )
    fills = _load_fills_safe(state_dir_path / "broker.json")
    recent_trips = _safe_list_of_dicts(attribution.get("recent_trips"))
    _effective_config_dir = config_dir if config_dir is not None else str(state_dir_path.parent)
    starting_cash = _load_starting_cash_safe(_effective_config_dir)
    venue_state, open_venues_list = _load_venue_open_state_safe(state_dir_path, _effective_config_dir)
    universe_symbols = _load_universe_symbols_safe(_effective_config_dir)
    company_map = _load_company_names(_effective_config_dir)
    return {
        **raw,
        "source": source,
        "starting_cash": starting_cash,
        "daemon_status": status if isinstance(status, dict) else {},
        "kpis": kpis
        if kpis
        else (raw.get("kpis") if isinstance(raw.get("kpis"), dict) else {}),
        "attribution": attribution
        if attribution
        else (
            raw.get("attribution") if isinstance(raw.get("attribution"), dict) else {}
        ),
        "equity_curve": equity_curve,
        "learnings": learnings,
        "trade_plans": trade_plans,
        # plans armés (D7 étage B) séparés des veilles simples : panneau dédié
        "armed_plans": [w for w in indicator_watches if _is_armed_plan(w)],
        "indicator_watches": indicator_watches,
        "stale_streaks": stale_streaks,
        "recent_decisions": recent_decisions,
        "consolidation_status": consolidation_status,
        "learnings_pending_count": learnings_pending_count,
        "fills": fills,
        "recent_trips": recent_trips,
        "venue_state": venue_state,
        "open_venues_list": open_venues_list,
        "universe_symbols": universe_symbols,
        "company_map": company_map,
    }
