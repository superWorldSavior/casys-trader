"""Gate de pertinence — D7 étage A : décider en code QUAND appeler le LLM.

Le LLM décideur était appelé par le planning pour constater l'absence
d'événement (~92 appels/24 h, ~99 % HOLD). Ce module filtre les réveils
PAR DÉFAUT (polling) sur symboles calmes ; il n'altère jamais :
- les réveils explicitement demandés par l'agent (autonomie de planification),
- les événements (triggers, position ouverte, régime de famille fort, signaux),
- la revue périodique garantie (l'agent revoit chaque symbole au moins toutes
  les ``max_quiet_hours``, et au premier réveil après un restart).

Fonctions pures : pas d'I/O, pas d'horloge.
"""

from __future__ import annotations

__all__ = ["cockpit_activity", "symbol_needs_llm"]


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
) -> tuple[bool, str]:
    """(faut-il appeler le LLM pour ce symbole dû, raison).

    Raisons possibles : agent_wake, trigger, position, regime, signal,
    periodic_review — et quiet (gate, pas d'appel).
    """
    if agent_requested_wake:
        return True, "agent_wake"
    if has_trigger:
        return True, "trigger"
    if has_position:
        return True, "position"
    if family_regime_strong:
        return True, "regime"
    if sig or stretched:
        return True, "signal"
    if hours_since_last_llm is None or hours_since_last_llm >= max_quiet_hours:
        return True, "periodic_review"
    return False, "quiet"


def cockpit_activity(cockpit: dict) -> dict[str, dict]:
    """Par symbole : ``{stretched, sig}`` extraits du cockpit compact (cp3).

    Tolère un cockpit dégradé (colonnes manquantes → valeurs None).
    """
    cols = cockpit.get("cols") or []
    rows = cockpit.get("rows") or []
    if "s" not in cols:
        return {}
    i_sym = cols.index("s")
    i_st = cols.index("st") if "st" in cols else None
    i_sig = cols.index("sig") if "sig" in cols else None

    out: dict[str, dict] = {}
    for row in rows:
        if len(row) <= i_sym:
            continue
        out[str(row[i_sym])] = {
            "stretched": row[i_st] if i_st is not None and len(row) > i_st else None,
            "sig": row[i_sig] if i_sig is not None and len(row) > i_sig else None,
        }
    return out
