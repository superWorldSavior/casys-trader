"""Gate de pertinence — D7 étage A : décider en code QUAND appeler le LLM.

Le LLM décideur était appelé par le planning pour constater l'absence
d'événement (~92 appels/24 h, ~99 % HOLD). Ce module filtre les réveils
PAR DÉFAUT (polling) sur symboles calmes et cadence les revues routinières
des positions ouvertes ; il n'altère jamais :
- les réveils explicitement demandés par l'agent (autonomie de planification),
- les triggers explicites (dont exit_watch),
- la revue périodique garantie (l'agent revoit chaque symbole au moins toutes
  les ``max_quiet_hours``, et au premier réveil après un restart).

Le 15m est du timing, pas une thèse : un sig 15m-only ou un stretch non aligné
ne réveille pas. Régime et signal HTF persistants gardent leur debounce 2 h ;
les positions et setups chauds ont leur cadence propre de 1 h en séance.

Fonctions pures : pas d'I/O, pas d'horloge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# Horizons thèse — même ordre que regime._HORIZON_PRIORITY hors 15m (timing).
THESIS_INTERVALS = ("1d", "4h")
TRIGGER_INTERVAL = "15m"
HOT_REVIEW_MAX_HOURS = 1.0
CALM_REVIEW_MAX_HOURS = 4.0
_HTF_SIG_PREFIXES = ("1d:", "4h:", "1h:")
SIGNAL_DEBOUNCE_HOURS = 2.0
POSITION_REVIEW_DEBOUNCE_HOURS = HOT_REVIEW_MAX_HOURS

STALE_REVIEW_FINGERPRINT_KEY = "stale_review"
STALE_REVIEW_PENDING = "pending"
FRESH_PROBE_WAKE_FINGERPRINT_KEY = "fresh_probe_wake"
FRESH_PROBE_INTERVAL_MINUTES = 15.0
HOT_REVIEW_WAKE_FINGERPRINT_KEY = "hot_review_wake"
REVIEWED_BAR_FINGERPRINT_KEY = "reviewed_bar"
REVIEWED_TRIGGER_FINGERPRINT_KEY = "reviewed_trigger"
FRESH_AFTER_STALE_REASON = "fresh_after_stale_review"
STALE_REPLAY_WAIT_REASON = "stale_replay_wait"
SAME_RUNTIME_BAR_REASON = "same_runtime_bar"
HOT_SETUP_REASON = "hot_setup"

__all__ = [
    "CALM_REVIEW_MAX_HOURS",
    "FRESH_AFTER_STALE_REASON",
    "FRESH_PROBE_INTERVAL_MINUTES",
    "FRESH_PROBE_WAKE_FINGERPRINT_KEY",
    "HOT_REVIEW_MAX_HOURS",
    "HOT_REVIEW_WAKE_FINGERPRINT_KEY",
    "HOT_SETUP_REASON",
    "POSITION_REVIEW_DEBOUNCE_HOURS",
    "REVIEWED_BAR_FINGERPRINT_KEY",
    "REVIEWED_TRIGGER_FINGERPRINT_KEY",
    "SAME_RUNTIME_BAR_REASON",
    "SIGNAL_DEBOUNCE_HOURS",
    "STALE_REVIEW_FINGERPRINT_KEY",
    "STALE_REVIEW_PENDING",
    "STALE_REPLAY_WAIT_REASON",
    "THESIS_INTERVALS",
    "TRIGGER_INTERVAL",
    "cockpit_activity",
    "execution_is_enabled",
    "is_pending_stale_review",
    "persistent_wake_fingerprints",
    "persistent_wake_reasons",
    "reviewed_bar_fingerprint",
    "reviewed_trigger_fingerprint",
    "session_is_open",
    "should_mark_stale_review_pending",
    "symbol_needs_llm",
]


def reviewed_bar_fingerprint(bar_ts: str) -> str:
    return f"{TRIGGER_INTERVAL}:{bar_ts}"


def reviewed_trigger_fingerprint(trigger_tokens: Iterable[str]) -> str | None:
    tokens = sorted({str(token).strip() for token in trigger_tokens if str(token).strip()})
    return "trigger:" + "|".join(tokens) if tokens else None


def is_pending_stale_review(fingerprints: Mapping[str, str] | None) -> bool:
    return (fingerprints or {}).get(STALE_REVIEW_FINGERPRINT_KEY) == STALE_REVIEW_PENDING


def session_is_open(execution: Mapping[str, object] | None) -> bool:
    """Session tradable : enabled, ou runtime_stale en séance ouverte."""
    if not execution:
        return True
    if isinstance(execution.get("session_open"), bool):
        return execution["session_open"] is True
    if execution.get("enabled") is True:
        return True
    return execution.get("reason") in {"runtime_stale", "no_price"}


def execution_is_enabled(execution: Mapping[str, object] | None) -> bool:
    return bool(execution) and execution.get("enabled") is True


def should_mark_stale_review_pending(execution: Mapping[str, object] | None) -> bool:
    if execution_is_enabled(execution):
        return False
    if not execution:
        return False
    return execution.get("reason") in {"runtime_stale", "no_price"}


def _has_htf_sig(sig: list | None) -> bool:
    if not sig:
        return False
    return any(str(token).startswith(_HTF_SIG_PREFIXES) for token in sig)


def _htf_material(*, sig: list | None, stretched: bool | None, aligned: bool | None) -> bool:
    if _has_htf_sig(sig):
        return True
    return stretched is True and aligned is True


def persistent_wake_reasons(
    *,
    family_regime_strong: bool,
    stretched: bool | None,
    sig: list | None,
    aligned: bool | None,
) -> tuple[str, ...]:
    """Raisons persistantes présentes maintenant (régime, signal HTF)."""
    present: list[str] = []
    if family_regime_strong:
        present.append("regime")
    if _htf_material(sig=sig, stretched=stretched, aligned=aligned):
        present.append("signal")
    return tuple(present)


def _signal_fingerprint(
    *, sig: list | None, stretched: bool | None, aligned: bool | None
) -> str | None:
    htf_tokens = sorted(
        {
            str(token).strip()
            for token in (sig or [])
            if str(token).strip().startswith(_HTF_SIG_PREFIXES)
        }
    )
    if htf_tokens:
        return "signal:" + "|".join(htf_tokens)
    if stretched is True and aligned is True:
        return "signal:stretched-aligned"
    return None


def persistent_wake_fingerprints(
    *,
    family_regime_fingerprint: str | None,
    stretched: bool | None,
    sig: list | None,
    aligned: bool | None,
) -> dict[str, str]:
    """Identity of material reasons; missing identity deliberately fails open."""
    fingerprints: dict[str, str] = {}
    if family_regime_fingerprint:
        fingerprints["regime"] = family_regime_fingerprint
    signal_fingerprint = _signal_fingerprint(
        sig=sig,
        stretched=stretched,
        aligned=aligned,
    )
    if signal_fingerprint:
        fingerprints["signal"] = signal_fingerprint
    return fingerprints


def _as_wake_reason_set(last_wake_reasons: Iterable[str] | str | None) -> frozenset[str]:
    if last_wake_reasons is None:
        return frozenset()
    if isinstance(last_wake_reasons, str):
        return frozenset((last_wake_reasons,))
    return frozenset(last_wake_reasons)


def _debounced(
    last_wake_reasons: Iterable[str] | str | None,
    reason: str,
    hours_since_last_llm: float | None,
    *,
    current_fingerprint: str | None,
    last_wake_fingerprints: Mapping[str, str] | None,
    debounce_hours: float = SIGNAL_DEBOUNCE_HOURS,
) -> bool:
    return (
        reason in _as_wake_reason_set(last_wake_reasons)
        and hours_since_last_llm is not None
        and hours_since_last_llm < debounce_hours
        and current_fingerprint is not None
        and (last_wake_fingerprints or {}).get(reason) == current_fingerprint
    )


def _disappeared_persistent_reason(
    *,
    current_fingerprints: Mapping[str, str],
    last_wake_fingerprints: Mapping[str, str] | None,
) -> str | None:
    """Return the first material reason that disappeared since the last review.

    Fingerprints are recorded only after a real LLM review.  For a held symbol,
    losing a previously reviewed regime or HTF signal is therefore a material
    transition, just like a new or inverted fingerprint.  Once that transition
    is reviewed, the daemon records the current (missing) fingerprint and the
    disappearance no longer wakes subsequent cycles.
    """
    previous = last_wake_fingerprints or {}
    for reason in ("regime", "signal"):
        if previous.get(reason) and reason not in current_fingerprints:
            return reason
    return None


def symbol_needs_llm(
    *,
    agent_requested_wake: bool,
    has_trigger: bool,
    has_position: bool,
    family_regime_strong: bool,
    stretched: bool | None,
    sig: list | None,
    hours_since_last_llm: float | None,
    max_quiet_hours: float = CALM_REVIEW_MAX_HOURS,
    aligned: bool | None = None,
    last_wake_reasons: set[str] | tuple[str, ...] | list[str] | None = None,
    family_regime_fingerprint: str | None = None,
    last_wake_fingerprints: Mapping[str, str] | None = None,
    session_open: bool = True,
    execution_enabled: bool = False,
    has_hot_setup: bool = False,
    current_runtime_bar_ts: str | None = None,
    current_trigger_fingerprint: str | None = None,
) -> tuple[bool, str]:
    """(faut-il appeler le LLM pour ce symbole dû, raison).

    Raisons possibles : agent_wake, fresh_after_stale_review, trigger, position,
    regime, signal, hot_setup, periodic_review — et quiet / position_debounce /
    same_runtime_bar (gate, pas d'appel).
    Debounce une raison persistante R ssi R est dans ``last_wake_reasons``
    et ``hours_since_last_llm`` est sous la cadence de séance.
    """
    stale_review_pending = is_pending_stale_review(last_wake_fingerprints)
    if stale_review_pending:
        if execution_enabled:
            return True, FRESH_AFTER_STALE_REASON
        if not agent_requested_wake:
            return False, STALE_REPLAY_WAIT_REASON
    if agent_requested_wake:
        return True, "agent_wake"

    debounce_hours = HOT_REVIEW_MAX_HOURS if session_open else CALM_REVIEW_MAX_HOURS
    position_deadline = debounce_hours
    hot_due = (
        session_open
        and hours_since_last_llm is not None
        and hours_since_last_llm >= HOT_REVIEW_MAX_HOURS
        and (has_position or has_hot_setup)
    )
    if has_trigger and current_trigger_fingerprint:
        last_trigger_fp = (last_wake_fingerprints or {}).get(
            REVIEWED_TRIGGER_FINGERPRINT_KEY
        )
        if last_trigger_fp == current_trigger_fingerprint and not hot_due:
            return False, SAME_RUNTIME_BAR_REASON

    if has_trigger:
        return True, "trigger"
    current_fingerprints = persistent_wake_fingerprints(
        family_regime_fingerprint=family_regime_fingerprint,
        stretched=stretched,
        sig=sig,
        aligned=aligned,
    )
    position_debounced = False
    if has_position:
        if hours_since_last_llm is None or hours_since_last_llm >= position_deadline:
            return True, "position"
        position_debounced = True
        disappeared_reason = _disappeared_persistent_reason(
            current_fingerprints=current_fingerprints,
            last_wake_fingerprints=last_wake_fingerprints,
        )
        if disappeared_reason is not None:
            return True, disappeared_reason
    if family_regime_strong and not _debounced(
        last_wake_reasons,
        "regime",
        hours_since_last_llm,
        current_fingerprint=current_fingerprints.get("regime"),
        last_wake_fingerprints=last_wake_fingerprints,
    ):
        return True, "regime"
    if _htf_material(sig=sig, stretched=stretched, aligned=aligned) and not _debounced(
        last_wake_reasons,
        "signal",
        hours_since_last_llm,
        current_fingerprint=current_fingerprints.get("signal"),
        last_wake_fingerprints=last_wake_fingerprints,
    ):
        return True, "signal"
    if (
        has_hot_setup
        and session_open
        and (hours_since_last_llm is None or hours_since_last_llm >= HOT_REVIEW_MAX_HOURS)
    ):
        return True, HOT_SETUP_REASON
    if position_debounced:
        return False, "position_debounce"
    if hours_since_last_llm is None or hours_since_last_llm >= max_quiet_hours:
        return True, "periodic_review"
    return False, "quiet"


def _cockpit_cell(row: list, cols: list, name: str):
    if name not in cols:
        return None
    index = cols.index(name)
    return row[index] if len(row) > index else None


def cockpit_activity(cockpit: dict) -> dict[str, dict]:
    """Par symbole : ``{stretched, sig, aligned, htf}`` extraits du cockpit compact (cp3).

    Tolère un cockpit dégradé (colonnes manquantes → valeurs None).
    """
    cols = cockpit.get("cols") or []
    rows = cockpit.get("rows") or []
    if "s" not in cols:
        return {}
    i_sym = cols.index("s")

    out: dict[str, dict] = {}
    for row in rows:
        if len(row) <= i_sym:
            continue
        out[str(row[i_sym])] = {
            "stretched": _cockpit_cell(row, cols, "st"),
            "sig": _cockpit_cell(row, cols, "sig"),
            "aligned": _cockpit_cell(row, cols, "aligned"),
            "htf": _cockpit_cell(row, cols, "htf"),
        }
    return out
