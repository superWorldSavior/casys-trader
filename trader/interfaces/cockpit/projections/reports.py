"""Collection/projection layer for the cockpit LLM reports gallery.

Reads report artifacts straight from the state directory (read-only) and
projects them into ordered, presentation-ready ``ReportItem`` rows:

- ``global``   : ``global_universe_postures/current.json`` (0-1 item)
- ``macro``    : ``news_briefs/latest-<VENUE>.jsonl`` (one JSON line per file)
- ``regional`` : ``universe_runs/latest-<VENUE>.json`` (dir may not exist)
- ``micro``    : ``company_intelligence/current/*.json`` envelopes — the most
  recent brief (by ``as_of``) among the depths present is kept.

TOTAL tolerance: a missing/corrupted artifact is skipped, nothing raises.
Ordering: fixed sections global → macro → regional → micro, ``as_of``
descending inside each section (None last).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text

from trader.interfaces.cockpit import format as f
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_MUTED,
)

KIND_ORDER: tuple[str, ...] = ("global", "macro", "regional", "micro")
_KIND_RANK = {kind: rank for rank, kind in enumerate(KIND_ORDER)}

# Pastille par kind : dégradé de luminance dans la palette casys. L'ambre
# (seule teinte structurelle) marque le rapport global ; vert/rouge restent
# réservés au P&L, warning à l'attention — jamais à l'identité d'un kind.
KIND_DOT_STYLES: dict[str, str] = {
    "global": CASYS_ACCENT,
    "macro": CASYS_MUTED,
    "regional": CASYS_DIM,
    "micro": CASYS_FG,
}

# Envelope depth keys iterated in this order — on an as_of tie the first one
# wins, so ``deep`` (the more informative brief) is preferred.
_DEPTH_PREFERENCE: tuple[str, ...] = ("deep", "screen")


@dataclass(frozen=True)
class ReportItem:
    """One gallery entry: a single LLM report artifact, presentation-ready."""

    kind: str  # "global" | "macro" | "regional" | "micro"
    key: str  # unique id ("global:current", "macro:EU", "regional:TW", "micro:AAPL")
    label: str  # short list text ("EU", "AAPL — ASML Holding")
    as_of: str | None
    depth: str | None  # micro only ("screen"/"deep"), else None
    payload: dict  # raw parsed JSON of the artifact (chosen brief for micro)


# ---------------------------------------------------------------------------
# Tolerant readers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    """JSON object from ``path``; None on any failure or non-dict content."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl_first(path: Path) -> dict | None:
    """First non-empty line of a .jsonl file as a dict; None on any failure."""

    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def _as_of(raw: object) -> str | None:
    text = str(raw or "").strip()
    return text or None


def _latest_failure(raw: object) -> dict | None:
    """Project current and legacy failure envelopes into one UI shape."""

    if not isinstance(raw, dict):
        return None
    nested = raw.get("latest_failure")
    if isinstance(nested, dict):
        return dict(nested)
    if raw.get("status") in {"error", "invalid"}:
        return dict(raw)
    failed_at = str(raw.get("last_failure_at") or "").strip()
    error = raw.get("last_error")
    if not failed_at or not isinstance(error, dict):
        return None
    return {
        "failed_at": failed_at,
        "error_code": str(error.get("code") or "unknown"),
        "error_message": str(error.get("message") or ""),
    }


def _with_latest_failure(payload: dict, failure: dict | None) -> dict:
    return {**payload, "latest_failure": failure} if failure is not None else payload


def _venue_suffix(path: Path, *, suffix: str) -> str:
    """``latest-EU.jsonl`` → ``EU`` (venue = filename suffix)."""

    return path.name.removeprefix("latest-").removesuffix(suffix).strip()


# ---------------------------------------------------------------------------
# Collectors — one per kind
# ---------------------------------------------------------------------------


def _history_count(state_dir: Path, as_of: object) -> int | None:
    """Non-empty line count of the ``YYYY-MM-DD.jsonl`` ledger for as_of's day."""

    parsed = f.parse_ts(as_of)
    if parsed is None:
        return None
    ledger = state_dir / "global_universe_postures" / f"{parsed:%Y-%m-%d}.jsonl"
    try:
        return sum(
            1
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except Exception:
        return None


def _collect_global(state_dir: Path, *, company_names: dict[str, str]) -> list[ReportItem]:
    payload = _read_json(state_dir / "global_universe_postures" / "current.json")
    failure = _latest_failure(
        _read_json(state_dir / "global_universe_postures" / "latest_failure.json")
    )
    if payload is None and failure is None:
        return []
    payload = _with_latest_failure(payload or dict(failure or {}), failure if payload else None)
    count = _history_count(state_dir, payload.get("as_of"))
    if count is not None and payload.get("status") not in {"error", "invalid"}:
        payload["history_count"] = count
    return [
        ReportItem(
            kind="global",
            key="global:current",
            label="Global",
            as_of=_as_of(payload.get("as_of")),
            depth=None,
            payload=payload,
        )
    ]


def _collect_macro(state_dir: Path, *, company_names: dict[str, str]) -> list[ReportItem]:
    items: list[ReportItem] = []
    payloads: dict[str, dict] = {}
    for path in sorted((state_dir / "news_briefs").glob("latest-*.jsonl")):
        venue = _venue_suffix(path, suffix=".jsonl")
        payload = _read_jsonl_first(path)
        if venue and payload is not None:
            payloads[venue] = payload

    status = _read_json(state_dir / "news_macro_analysis_status.json") or {}
    failures = {
        str(venue).strip(): failure
        for venue, raw in status.items()
        if str(venue).strip() and (failure := _latest_failure(raw)) is not None
    }
    for venue in sorted(set(payloads) | set(failures)):
        base_payload = payloads[venue] if venue in payloads else dict(failures[venue])
        payload = _with_latest_failure(base_payload, failures.get(venue))
        items.append(
            ReportItem(
                kind="macro",
                key=f"macro:{venue}",
                label=venue,
                as_of=_as_of(payload.get("as_of") or payload.get("failed_at")),
                depth=None,
                payload=payload,
            )
        )
    return items


def _collect_regional(state_dir: Path, *, company_names: dict[str, str]) -> list[ReportItem]:
    items: list[ReportItem] = []
    # universe_runs/ may legitimately not exist — glob then yields nothing.
    for path in sorted((state_dir / "universe_runs").glob("latest-*.json")):
        venue = _venue_suffix(path, suffix=".json")
        if not venue:
            continue
        payload = _read_json(path)
        if payload is None:
            continue
        items.append(
            ReportItem(
                kind="regional",
                key=f"regional:{venue}",
                label=venue,
                as_of=_as_of(payload.get("as_of")),
                depth=None,
                payload=payload,
            )
        )
    return items


def _pick_brief(envelope: dict) -> tuple[dict | None, str | None]:
    """Most recent brief (by as_of) among the depths present in the envelope.

    Ties (or missing as_of everywhere) resolve by ``_DEPTH_PREFERENCE`` order
    — ``deep`` wins over ``screen``.
    """

    briefs = f.safe_dict(envelope.get("briefs"))
    ordered = [depth for depth in _DEPTH_PREFERENCE if depth in briefs]
    ordered += sorted(depth for depth in briefs if depth not in _DEPTH_PREFERENCE)

    best_brief: dict | None = None
    best_depth: str | None = None
    best_key: tuple | None = None
    for depth_key in ordered:
        brief = briefs.get(depth_key)
        if not isinstance(brief, dict):
            continue
        parsed = f.parse_ts(brief.get("as_of"))
        sort_key = (parsed is not None, parsed.timestamp() if parsed else 0.0)
        if best_key is None or sort_key > best_key:
            best_key = sort_key
            best_brief = brief
            best_depth = str(brief.get("depth") or depth_key)
    return best_brief, best_depth


def _collect_micro(state_dir: Path, *, company_names: dict[str, str]) -> list[ReportItem]:
    items: list[ReportItem] = []
    failures: dict[str, dict] = {}
    for path in sorted((state_dir / "company_analysis_runs" / "latest").glob("*.json")):
        run = _read_json(path)
        symbol = str((run or {}).get("symbol") or "").strip()
        failure = _latest_failure(run)
        if symbol and failure is not None:
            failures[symbol] = failure

    seen: set[str] = set()
    for path in sorted((state_dir / "company_intelligence" / "current").glob("*.json")):
        envelope = _read_json(path)
        if envelope is None:
            continue
        symbol = str(envelope.get("symbol") or "").strip()
        if not symbol:
            continue
        brief, depth = _pick_brief(envelope)
        if brief is None:
            continue
        seen.add(symbol)
        brief = _with_latest_failure(brief, failures.get(symbol))
        name = str(company_names.get(symbol) or "").strip()
        label = f"{symbol} — {name}" if name else symbol
        items.append(
            ReportItem(
                kind="micro",
                key=f"micro:{symbol}",
                label=label,
                as_of=_as_of(brief.get("as_of")),
                depth=depth,
                payload=brief,
            )
        )
    for symbol in sorted(set(failures) - seen):
        failure = failures[symbol]
        name = str(company_names.get(symbol) or "").strip()
        items.append(
            ReportItem(
                kind="micro",
                key=f"micro:{symbol}",
                label=f"{symbol} — {name}" if name else symbol,
                as_of=_as_of(failure.get("as_of") or failure.get("failed_at")),
                depth=str(failure.get("depth") or "") or None,
                payload=failure,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _sort_key(item: ReportItem) -> tuple:
    parsed = f.parse_ts(item.as_of)
    return (
        _KIND_RANK.get(item.kind, len(KIND_ORDER)),
        parsed is None,  # None as_of last inside each section
        -parsed.timestamp() if parsed else 0.0,
        item.key,
    )


def collect_report_items(
    state_dir: Path,
    *,
    company_names: dict[str, str] | None = None,
) -> list[ReportItem]:
    """Collect every available report artifact under ``state_dir``.

    Sections are fixed (global → macro → regional → micro); inside each
    section items sort by ``as_of`` descending, None last. Never raises:
    a missing directory or corrupted artifact simply yields no item.
    """

    names = dict(company_names or {})
    items: list[ReportItem] = []
    for collector in (_collect_global, _collect_macro, _collect_regional, _collect_micro):
        try:
            items.extend(collector(state_dir, company_names=names))
        except Exception:
            continue
    items.sort(key=_sort_key)
    return items


def _relative_age(as_of: object, *, now: datetime) -> str:
    """Age of the report : "45m" / "3h" / "2d", "—" when unknown."""

    parsed = f.parse_ts(as_of)
    if parsed is None:
        return "—"
    reference = now if now.tzinfo else now.replace(tzinfo=UTC)
    minutes = max(0, int((reference - parsed).total_seconds())) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def build_gallery_row(item: ReportItem, *, now: datetime) -> Text:
    """One list row: kind dot (palette color per kind), label, relative age."""

    row = Text()
    row.append("● ", style=KIND_DOT_STYLES.get(item.kind, CASYS_FAINT))
    row.append(item.label, style=f"bold {CASYS_FG}")
    row.append("  ")
    row.append(_relative_age(item.as_of, now=now), style=CASYS_FAINT)
    return row


__all__ = [
    "KIND_DOT_STYLES",
    "KIND_ORDER",
    "ReportItem",
    "build_gallery_row",
    "collect_report_items",
]
