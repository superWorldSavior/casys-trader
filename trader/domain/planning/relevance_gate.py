"""Gate de pertinence — D7 étage A : décider en code QUAND appeler le LLM.

Le LLM décideur était appelé par le planning pour constater l'absence
d'événement (~92 appels/24 h, ~99 % HOLD). Ce module filtre les réveils
PAR DÉFAUT (polling) sur symboles calmes ; il n'altère jamais :
- les réveils explicitement demandés par l'agent (autonomie de planification),
- les événements (triggers, position ouverte, régime de famille fort, signaux HTF),
- la revue périodique garantie (l'agent revoit chaque symbole au moins toutes
  les ``max_quiet_hours``, et au premier réveil après un restart).

Le 15m est du timing, pas une thèse : un sig 15m-only ou un stretch non aligné
ne réveille pas. Régime et signal HTF persistants sont débouncés 2 h (D7 backlog).

Fonctions pures : pas d'I/O, pas d'horloge.
"""

from __future__ import annotations

from collections.abc import Iterable

# Horizons thèse — même ordre que regime._HORIZON_PRIORITY hors 15m (timing).
_HTF_SIG_PREFIXES = ("1d:", "4h:", "1h:")
SIGNAL_DEBOUNCE_HOURS = 2.0

__all__ = [
    "SIGNAL_DEBOUNCE_HOURS",
    "cockpit_activity",
    "persistent_wake_reasons",
    "symbol_needs_llm",
]


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
) -> bool:
    return (
        reason in _as_wake_reason_set(last_wake_reasons)
        and hours_since_last_llm is not None
        and hours_since_last_llm < SIGNAL_DEBOUNCE_HOURS
    )


def symbol_needs_llm(
    *,
    agent_requested_wake: bool,
    has_trigger: bool,
    has_position: bool,
    family_regime_strong: bool,
    stretched: bool | None,
    sig: list | None,
    hours_since_last_llm: float | None,
    max_quiet_hours: float = 4.0,
    aligned: bool | None = None,
    last_wake_reasons: set[str] | tuple[str, ...] | list[str] | None = None,
) -> tuple[bool, str]:
    """(faut-il appeler le LLM pour ce symbole dû, raison).

    Raisons possibles : agent_wake, trigger, position, regime, signal,
    periodic_review — et quiet (gate, pas d'appel).
    Debounce une raison persistante R ssi R est dans ``last_wake_reasons``
    et ``hours_since_last_llm < SIGNAL_DEBOUNCE_HOURS``.
    """
    if agent_requested_wake:
        return True, "agent_wake"
    if has_trigger:
        return True, "trigger"
    if has_position:
        return True, "position"
    if family_regime_strong and not _debounced(
        last_wake_reasons, "regime", hours_since_last_llm
    ):
        return True, "regime"
    if _htf_material(sig=sig, stretched=stretched, aligned=aligned) and not _debounced(
        last_wake_reasons, "signal", hours_since_last_llm
    ):
        return True, "signal"
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
