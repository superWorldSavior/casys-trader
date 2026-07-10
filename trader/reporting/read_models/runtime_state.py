"""Runtime read model assembly for the casys-trader UI."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trader.planning.indicator_watch import is_armed_plan as _is_armed_plan
from trader.support.config.risk import (
    DEFAULT_MIN_TRADE_CONFIDENCE,
    read_min_trade_confidence,
)

# Racine du repo (quatre niveaux au-dessus de ce fichier)
_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIR = _ROOT / "state"
_CURRENT_REPORT_FILE = _STATE_DIR / "current_report.json"
_LAST_REPORT_FILE = _STATE_DIR / "last_report.json"
_STATUS_FILE = _STATE_DIR / "daemon_status.json"
UTC = timezone.utc
_UNIVERSE_PIPELINE_VENUES = ("TW", "EU", "US")


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
        from trader.reporting.read_models.live_kpis import compute_live_kpis

        result = compute_live_kpis(state_dir)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _read_min_trade_confidence_safe() -> float:
    """Lit min_trade_confidence depuis config/risk.yaml via le lecteur canonique."""
    try:
        return read_min_trade_confidence(_ROOT / "config" / "risk.yaml")
    except Exception:
        return DEFAULT_MIN_TRADE_CONFIDENCE


def _compute_attribution_safe(state_dir: Path) -> dict:
    try:
        from trader.reporting.read_models.attribution import compute_attribution

        result = compute_attribution(
            state_dir,
            min_entry_confidence=_read_min_trade_confidence_safe(),
        )
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _load_learnings_safe(state_dir: Path, *, limit: int = 5) -> list[dict]:
    try:
        from trader.agent.learnings.raw_store import RawLearningsStore

        return _safe_list_of_dicts(RawLearningsStore(state_dir / "learnings.jsonl").recent(limit=limit))
    except Exception:
        return []


def _load_trade_plans_safe(plans_path: Path) -> list[dict]:
    """Lit les plans ouverts depuis SQLite, avec fallback JSON de tests.

    ``casys.db`` est la vérité si présent. Si la base est absente, conserve la
    lecture du shadow ``trade_plans.json`` pour les fixtures et vieux états.
    """
    state_dir = plans_path.parent
    db_path = state_dir / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore

            db = open_state_db(db_path)
            return [p.model_dump() for p in SqliteTradePlanStore(db).open_plans()]
        except Exception:
            return []

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


def _load_scheduler_data_safe(scheduler_path: Path, *, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Lit scheduler.json, retourne (indicator_watches, stale_streaks).

    indicator_watches : liste de dicts (valeurs du dict indicator_watches),
                        veilles expirées exclues (cohérent avec le daemon).
    stale_streaks     : dict {symbol: int}.
    Tolérant : retourne ([], {}) si absent/corrompu.
    """
    now = now or datetime.now(UTC)
    db_path = scheduler_path.parent / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

            scheduler = SqliteScheduler(open_state_db(db_path))
            candidates = [v for v in scheduler.watches().values() if isinstance(v, dict)]
            watches = [w for w in candidates if not _watch_is_expired(w, now)]
            return watches, scheduler.stale_streaks()
        except Exception:
            return [], {}

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


def _load_scheduler_wakes_safe(scheduler_path: Path) -> tuple[str | None, dict]:
    """Lit scheduler.json, retourne (default_next_wake, symbol_wakes).

    default_next_wake : ISO str du prochain réveil global, None si absent.
    symbol_wakes      : dict {symbol: ISO str du prochain réveil}.
    Tolérant : retourne (None, {}) si absent/corrompu.
    """
    db_path = scheduler_path.parent / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

            return SqliteScheduler(open_state_db(db_path)).wakes()
        except Exception:
            return None, {}

    try:
        raw = json.loads(scheduler_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None, {}
        # legacy : Scheduler._load_state promeut next_wake → default_next_wake
        default_next_wake = raw.get("default_next_wake") or raw.get("next_wake")
        default_next_wake = str(default_next_wake) if default_next_wake else None
        symbols = raw.get("symbols") or {}
        if not isinstance(symbols, dict):
            symbols = {}
        symbol_wakes = {str(k): str(v) for k, v in symbols.items() if v}
        return default_next_wake, symbol_wakes
    except Exception:
        return None, {}


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


def _enrich_decisions_with_data_source(decisions: list[dict], recent_decisions: list[dict]) -> list[dict]:
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
    db_path = broker_path.parent / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.broker_store import SqliteBroker
            from trader.infrastructure.state_db.connection import open_state_db

            return _safe_list_of_dicts(SqliteBroker(open_state_db(db_path)).fills()[-limit:])
        except Exception:
            return []

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


def _load_venue_open_state_safe(state_dir: Path, config_dir: str) -> tuple[dict, list[str], dict]:
    """Charge venue_state, open_venues et sessions. ({}, [], {}) si indisponible."""
    try:
        from trader.market.rotation.venues import load_venue_state as _lvs
        from trader.market.rotation.schedule import load_sessions as _ls, open_venues as _ov

        venue_state = _lvs(state_dir)
        sessions = _ls(config_dir)
        now_iso = datetime.now(UTC).isoformat()
        ov_list = _ov(now_iso, sessions)
        return venue_state, ov_list, sessions
    except Exception:
        return {}, [], {}


def _count_pending_learnings_safe(learnings_path: Path, consolidated_path: Path) -> int:
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
        from trader.agent.learnings.selection import pending_raw_count

        return pending_raw_count(raw_rows, watermark)
    except Exception:
        return 0


def _load_queue_worker_activity_safe(task_ledger_path: Path) -> dict:
    """Résumé compact des workers decide actifs depuis task_ledger.db."""
    if not task_ledger_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{task_ledger_path}?mode=ro", uri=True, timeout=0.2)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS running_tasks,
                  COUNT(DISTINCT claimed_by) AS active_workers
                FROM tasks
                WHERE kind='decide'
                  AND status='running'
                  AND claimed_by IS NOT NULL
                """
            ).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind='decide' AND status='pending'"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    return {
        "active_workers": int(row["active_workers"] or 0) if row else 0,
        "running_tasks": int(row["running_tasks"] or 0) if row else 0,
        "pending_tasks": int(pending[0] or 0) if pending else 0,
    }


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
        from trader.support.config.portfolio import load_starting_cash

        return load_starting_cash(Path(config_dir) / "config")
    except Exception:
        return None


def _read_projection_safe(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read one current/latest projection without breaking the cockpit.

    The brief projection is a one-line JSONL file while the other projections
    are JSON objects. Reading the last valid non-empty line supports both forms
    and still reports a present-but-unreadable file as ``unavailable``.
    """

    try:
        if not path.exists():
            return None, "pending"
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None, "unavailable"
    for line in reversed(content.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload, "available"
    return None, "unavailable"


def _short_pipeline_id(value: Any) -> str | None:
    """Return a compact operator label while retaining the full ID alongside it."""

    text = str(value or "").strip()
    if not text:
        return None
    tail = text.rsplit(":", 1)[-1]
    if len(tail) <= 12:
        return tail
    return f"{tail[:6]}…{tail[-4:]}"


def _safe_pipeline_count(value: Any) -> int:
    number = _safe_float(value, default=None)
    return max(0, int(number)) if number is not None else 0


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if item]


def _candidate_is_challenger(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    if isinstance(candidate.get("fresh_news"), dict):
        return True
    if candidate.get("candidate_source") == "fresh_news":
        return True
    sources = candidate.get("candidate_sources")
    return isinstance(sources, (list, tuple, set)) and "fresh_news" in sources


def _brief_point_count(payload: dict[str, Any]) -> int:
    total = len(_safe_list_of_dicts(payload.get("alerts")))
    for section_key in ("zones", "families", "symbols"):
        sections = payload.get(section_key)
        if isinstance(sections, dict):
            total += sum(len(_safe_list_of_dicts(points)) for points in sections.values())
        elif isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict):
                    total += len(_safe_list_of_dicts(section.get("points")))
    return total


def _latest_activations_by_venue_safe(
    ledger_path: Path,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Return the latest activation independently for each venue."""

    try:
        if not ledger_path.exists():
            return {}, "pending"
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}, "unavailable"

    activations: dict[str, dict[str, Any]] = {}
    valid_rows = 0
    for line in reversed(lines):
        if len(activations) == len(_UNIVERSE_PIPELINE_VENUES):
            break
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        valid_rows += 1
        overrides = row.get("overrides")
        overrides = overrides if isinstance(overrides, dict) else {}
        venue = str(overrides.get("venue") or row.get("venue") or "").strip().upper()
        if venue in _UNIVERSE_PIPELINE_VENUES and venue not in activations:
            activations[venue] = row
    return activations, ("available" if valid_rows else "unavailable")


def _scope_pipeline_entry(
    *,
    venue: str,
    projection: dict[str, Any] | None,
    projection_status: str,
    venue_entry: dict[str, Any],
) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    venue_has_scope = bool(
        venue_entry.get("candidate_scope_id") or venue_entry.get("candidates") or venue_entry.get("default_hotlist")
    )
    payload = projection or (venue_entry if venue_has_scope else {})
    source = "projection" if projection is not None else ("venue_state" if venue_has_scope else None)
    scope_id = str(payload.get("candidate_scope_id") or "").strip() or None
    candidates = _safe_list_of_dicts(payload.get("candidates"))
    hotlist = _safe_string_list(payload.get("default_hotlist"))
    challenger_count = sum(1 for candidate in candidates if _candidate_is_challenger(candidate))
    if source is not None:
        status = "ready" if scope_id else "unavailable"
    else:
        status = projection_status
    return {
        "status": status,
        "projection_status": projection_status,
        "source": source,
        "id": scope_id,
        "id_short": _short_pipeline_id(scope_id),
        "as_of": payload.get("as_of") or payload.get("last_close_at"),
        "candidate_run_ids": _safe_string_list(payload.get("candidate_run_ids")),
        "candidate_count": len(candidates),
        "baseline_hotlist_count": len(hotlist),
        "challenger_count": challenger_count,
    }


def _scout_pipeline_entry(*, venue: str, projection: dict[str, Any] | None, projection_status: str) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    if projection is None:
        return {
            "status": projection_status,
            "id": None,
            "id_short": None,
            "challenger_count": 0,
            "coverage": {},
        }
    run_id = str(projection.get("candidate_run_id") or "").strip() or None
    challengers = _safe_list_of_dicts(projection.get("challengers"))
    coverage = {
        "status": projection.get("coverage_status"),
        "eligible_symbol_count": _safe_pipeline_count(projection.get("eligible_symbol_count")),
        "radar_symbol_count": _safe_pipeline_count(projection.get("radar_symbol_count")),
        "archived_items_read": _safe_pipeline_count(projection.get("archived_items_read")),
        "items_read": _safe_pipeline_count(projection.get("items_read")),
        "eligible_items": _safe_pipeline_count(projection.get("eligible_items")),
        "rejection_counts": (
            dict(projection.get("rejection_counts")) if isinstance(projection.get("rejection_counts"), dict) else {}
        ),
    }
    return {
        "status": str(projection.get("status") or "available"),
        "id": run_id,
        "id_short": _short_pipeline_id(run_id),
        "as_of": projection.get("as_of"),
        "reason": projection.get("reason"),
        "challenger_count": len(challengers),
        "challengers": [str(item.get("symbol")) for item in challengers if item.get("symbol")],
        "coverage": coverage,
    }


def _brief_pipeline_entry(
    *,
    venue: str,
    projection: dict[str, Any] | None,
    projection_status: str,
    scope_id: str | None,
) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    if projection is None:
        return {
            "status": projection_status,
            "id": None,
            "id_short": None,
            "scope_match": None,
            "point_count": 0,
            "alert_count": 0,
            "coverage": {},
        }
    brief_id = str(projection.get("brief_id") or "").strip() or None
    input_refs = projection.get("input_refs")
    input_refs = input_refs if isinstance(input_refs, dict) else {}
    brief_scope_id = str(input_refs.get("candidate_scope_id") or "").strip() or None
    scope_match = bool(scope_id and brief_scope_id and scope_id == brief_scope_id)
    if scope_id is None or brief_scope_id is None:
        scope_match = None
    status = "scope_mismatch" if scope_match is False else "ready"
    coverage = input_refs.get("coverage")
    return {
        "status": status,
        "id": brief_id,
        "id_short": _short_pipeline_id(brief_id),
        "as_of": projection.get("as_of"),
        "valid_until": projection.get("valid_until"),
        "scope_id": brief_scope_id,
        "scope_id_short": _short_pipeline_id(brief_scope_id),
        "scope_match": scope_match,
        "point_count": _brief_point_count(projection),
        "alert_count": len(_safe_list_of_dicts(projection.get("alerts"))),
        "coverage": dict(coverage) if isinstance(coverage, dict) else {},
        "ref": {
            "date": str(projection.get("as_of") or "")[:10],
            "venue": venue,
            "brief_id": brief_id,
            "as_of": projection.get("as_of"),
        },
    }


def _agent_pipeline_entry(*, venue: str, projection: dict[str, Any] | None, projection_status: str) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    if projection is None:
        return {
            "status": projection_status,
            "id": None,
            "id_short": None,
            "hotlist_count": 0,
            "challenger_count": 0,
            "coverage": {},
        }
    run_id = str(projection.get("agent_run_id") or "").strip() or None
    hotlist = _safe_string_list(projection.get("selected_hotlist"))
    challengers = _safe_string_list(projection.get("selected_challengers"))
    coverage = projection.get("coverage")
    return {
        "status": str(projection.get("status") or "available"),
        "id": run_id,
        "id_short": _short_pipeline_id(run_id),
        "as_of": projection.get("as_of"),
        "scope_id": projection.get("candidate_scope_id"),
        "scope_id_short": _short_pipeline_id(projection.get("candidate_scope_id")),
        "brief_ref": projection.get("brief_ref"),
        "error_code": projection.get("error_code"),
        "provider": projection.get("agent_provider"),
        "model": projection.get("agent_model"),
        "provider_fallback_reason": projection.get("agent_provider_fallback_reason"),
        "hotlist_count": len(hotlist),
        "selected_hotlist": hotlist,
        "challenger_count": len(challengers),
        "selected_challengers": challengers,
        "coverage": dict(coverage) if isinstance(coverage, dict) else {},
    }


def _activation_pipeline_entry(
    *,
    venue: str,
    ledger_row: dict[str, Any] | None,
    ledger_status: str,
    venue_entry: dict[str, Any],
) -> dict[str, Any]:
    if ledger_row is not None:
        overrides = ledger_row.get("overrides")
        overrides = overrides if isinstance(overrides, dict) else {}
        hotlist = overrides.get("selected_hotlist") or ledger_row.get("final_hot_set") or []
        challengers = overrides.get("selected_challengers") or []
        fallback_used = bool(overrides.get("fallback_used", False))
        fallback_reason = overrides.get("fallback_reason")
        scope_id = overrides.get("candidate_scope_id")
        agent_run_id = overrides.get("agent_run_id")
        brief_ref = overrides.get("brief_ref")
        has_universe_lineage = any(
            key in overrides
            for key in ("candidate_scope_id", "agent_run_id", "brief_ref", "fallback_used")
        )
        return {
            "status": (
                "fallback" if fallback_used else ("activated" if has_universe_lineage else "legacy")
            ),
            "source": "rotation_ledger",
            "as_of": ledger_row.get("as_of"),
            "scope_id": scope_id,
            "scope_id_short": _short_pipeline_id(scope_id),
            "agent_run_id": agent_run_id,
            "agent_run_id_short": _short_pipeline_id(agent_run_id),
            "brief_ref": brief_ref,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "hotlist_count": len(hotlist) if isinstance(hotlist, list) else 0,
            "selected_hotlist": list(hotlist) if isinstance(hotlist, list) else [],
            "challenger_count": len(challengers) if isinstance(challengers, list) else 0,
            "selected_challengers": list(challengers) if isinstance(challengers, list) else [],
        }

    venue_has_activation = bool(
        venue_entry.get("last_universe_activation_scope_id")
        or venue_entry.get("last_universe_attempt_token")
    )
    if venue_has_activation:
        hotlist = venue_entry.get("hotlist") or []
        fallback_used = bool(venue_entry.get("last_universe_fallback_used", False))
        scope_id = venue_entry.get("last_universe_activation_scope_id") or venue_entry.get(
            "candidate_scope_id"
        )
        agent_run_id = venue_entry.get("last_universe_agent_run_id")
        return {
            "status": "fallback" if fallback_used else "activated",
            "source": "venue_state",
            "as_of": venue_entry.get("last_universe_activation_at")
            or venue_entry.get("last_universe_attempt_at"),
            "scope_id": scope_id,
            "scope_id_short": _short_pipeline_id(scope_id),
            "agent_run_id": agent_run_id,
            "agent_run_id_short": _short_pipeline_id(agent_run_id),
            "brief_ref": venue_entry.get("last_universe_brief_ref"),
            "fallback_used": fallback_used,
            "fallback_reason": venue_entry.get("last_universe_fallback_reason"),
            "hotlist_count": len(hotlist) if isinstance(hotlist, list) else 0,
            "selected_hotlist": list(hotlist) if isinstance(hotlist, list) else [],
            "challenger_count": 0,
            "selected_challengers": [],
        }

    return {
        "status": ledger_status if ledger_status == "unavailable" else "pending",
        "source": None,
        "as_of": None,
        "scope_id": None,
        "scope_id_short": None,
        "agent_run_id": None,
        "agent_run_id_short": None,
        "brief_ref": None,
        "fallback_used": False,
        "fallback_reason": None,
        "hotlist_count": 0,
        "selected_hotlist": [],
        "challenger_count": 0,
        "selected_challengers": [],
    }


def _load_universe_pipeline_safe(state_dir: Path, venue_state: dict[str, Any]) -> dict[str, dict]:
    """Assemble the current operator projection for each equity venue."""

    venues_state = venue_state.get("venues") if isinstance(venue_state, dict) else {}
    venues_state = venues_state if isinstance(venues_state, dict) else {}
    activations, ledger_status = _latest_activations_by_venue_safe(state_dir / "rotation_ledger.jsonl")
    pipeline: dict[str, dict] = {}
    for venue in _UNIVERSE_PIPELINE_VENUES:
        venue_entry = venues_state.get(venue)
        venue_entry = venue_entry if isinstance(venue_entry, dict) else {}
        scope_raw, scope_projection_status = _read_projection_safe(
            state_dir / "candidate_scopes" / f"current-{venue}.json"
        )
        scout_raw, scout_projection_status = _read_projection_safe(
            state_dir / "news_challenger_runs" / f"latest-{venue}.json"
        )
        brief_raw, brief_projection_status = _read_projection_safe(state_dir / "news_briefs" / f"latest-{venue}.jsonl")
        agent_raw, agent_projection_status = _read_projection_safe(state_dir / "universe_runs" / f"latest-{venue}.json")
        scope = _scope_pipeline_entry(
            venue=venue,
            projection=scope_raw,
            projection_status=scope_projection_status,
            venue_entry=venue_entry,
        )
        pipeline[venue] = {
            "venue": venue,
            "scope": scope,
            "scout": _scout_pipeline_entry(
                venue=venue,
                projection=scout_raw,
                projection_status=scout_projection_status,
            ),
            "brief": _brief_pipeline_entry(
                venue=venue,
                projection=brief_raw,
                projection_status=brief_projection_status,
                scope_id=scope.get("id"),
            ),
            "agent": _agent_pipeline_entry(
                venue=venue,
                projection=agent_raw,
                projection_status=agent_projection_status,
            ),
            "activation": _activation_pipeline_entry(
                venue=venue,
                ledger_row=activations.get(venue),
                ledger_status=ledger_status,
                venue_entry=venue_entry,
            ),
        }
    return pipeline


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
        Path(current_report_path) if current_report_path is not None else state_dir_path / "current_report.json"
    )
    last_report_path = Path(last_report_path) if last_report_path is not None else state_dir_path / "last_report.json"
    status_path = Path(status_path) if status_path is not None else state_dir_path / "daemon_status.json"

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
    indicator_watches, stale_streaks = _load_scheduler_data_safe(state_dir_path / "scheduler.json")
    default_next_wake, symbol_wakes = _load_scheduler_wakes_safe(state_dir_path / "scheduler.json")
    recent_decisions = _tail_decisions_safe(state_dir_path / "decisions.jsonl", n=50)
    consolidation_status = _load_consolidation_status_safe(state_dir_path / "learnings_consolidation_status.json")
    queue_worker_activity = _load_queue_worker_activity_safe(state_dir_path / "task_ledger.db")
    learnings_pending_count = _count_pending_learnings_safe(
        state_dir_path / "learnings.jsonl",
        state_dir_path / "learnings_consolidated.json",
    )
    fills = _load_fills_safe(state_dir_path / "broker.json")
    recent_trips = _safe_list_of_dicts(attribution.get("recent_trips"))
    _effective_config_dir = config_dir if config_dir is not None else str(state_dir_path.parent)
    starting_cash = _load_starting_cash_safe(_effective_config_dir)
    venue_state, open_venues_list, sessions = _load_venue_open_state_safe(state_dir_path, _effective_config_dir)
    universe_pipeline = _load_universe_pipeline_safe(state_dir_path, venue_state)
    radar_score_audit = load_state(state_dir_path / "radar_score_audit.json")
    radar_score_bench = load_state(state_dir_path / "radar_score_bench.json")
    universe_symbols = _load_universe_symbols_safe(_effective_config_dir)
    company_map = _load_company_names(_effective_config_dir)
    return {
        **raw,
        "source": source,
        "starting_cash": starting_cash,
        "daemon_status": status if isinstance(status, dict) else {},
        "kpis": kpis if kpis else (raw.get("kpis") if isinstance(raw.get("kpis"), dict) else {}),
        "attribution": attribution
        if attribution
        else (raw.get("attribution") if isinstance(raw.get("attribution"), dict) else {}),
        "equity_curve": equity_curve,
        "learnings": learnings,
        "trade_plans": trade_plans,
        # plans armés (D7 étage B) séparés des veilles simples : panneau dédié
        "armed_plans": [w for w in indicator_watches if _is_armed_plan(w)],
        "indicator_watches": indicator_watches,
        "stale_streaks": stale_streaks,
        # réveils du scheduler : countdown NEXT WAKE (global) + par symbole
        "default_next_wake": default_next_wake,
        "symbol_wakes": symbol_wakes,
        "recent_decisions": recent_decisions,
        "consolidation_status": consolidation_status,
        "queue_worker_activity": queue_worker_activity,
        "learnings_pending_count": learnings_pending_count,
        "fills": fills,
        "recent_trips": recent_trips,
        "venue_state": venue_state,
        "universe_pipeline": universe_pipeline,
        "radar_score_audit": radar_score_audit if isinstance(radar_score_audit, dict) else {},
        "radar_score_bench": radar_score_bench if isinstance(radar_score_bench, dict) else {},
        "open_venues_list": open_venues_list,
        "sessions": sessions,
        "universe_symbols": universe_symbols,
        "company_map": company_map,
    }
