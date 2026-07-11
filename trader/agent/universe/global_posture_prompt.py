"""Prompt and parser for the global universe posture (PM global level).

The universe role runs once in a *global* mode: from the comparative family board,
the global macro digest and current engagements (sticky), it decides a cross-region
stance. The stance is advisory — a frame for the regional passes, never a quota, a
capital allocation or an order. No symbol is selected here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from trader.domain.universe.global_posture import GlobalUniversePosture


def build_global_posture_prompt(
    *,
    global_family_board: Any,
    global_situation_digest: Any,
    sticky: Iterable[str],
    venues: Iterable[str],
    as_of: str,
) -> str:
    """Render the bounded global-posture request for the universe agent."""

    payload = {
        "as_of": as_of,
        "venues": list(venues),
        "sticky": list(sticky),
        "global_situation_digest": global_situation_digest,
        "global_family_board": global_family_board,
    }
    return (
        "Tu es l'agent univers de Casys Trader, en mode GLOBAL (niveau PM cross-région).\n"
        "Tu ne sélectionnes AUCUN symbole ici. Tu décides une POSTURE comparative entre les "
        "venues (TW/EU/US) et les familles, à partir du board comparatif, du digest macro "
        "global et des engagements actuels (sticky).\n"
        "Cette posture est un CADRE advisory pour les passes régionales qui choisiront ensuite "
        "leurs symboles — jamais un quota de places, une allocation de capital, ni un ordre.\n"
        "Retourne uniquement un objet JSON valide avec ce schéma:\n"
        '{"venue_posture":{"TW":"favor|selective|watch|avoid","EU":"...","US":"..."},'
        '"family_priority":{"favored":["famille"],"deprioritized":["famille"]},'
        '"gross_mode":"normal|cautious|risk_off","net_bias":"long|short|neutral",'
        '"rationale":"..."}\n'
        "venue_posture couvre chaque venue de `venues`. family_priority nomme les familles à "
        "privilégier ou déprioriser cross-région (bornées, celles du board). gross_mode et "
        "net_bias = ton inclinaison d'exposition globale (advisory). rationale = 1 à 3 phrases "
        "justifiant les arbitrages à partir des faits fournis; n'invente aucune donnée absente.\n"
        "JSON d'entrée borné:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def parse_global_posture_completion(
    text: str,
) -> tuple[GlobalUniversePosture | None, str | None]:
    """Parse the global-posture completion into a bounded domain artefact."""

    payload = _extract_last_json_object(text)
    if payload is None:
        return None, "invalid_json"
    posture = GlobalUniversePosture.from_mapping(payload)
    if posture is None:
        return None, "invalid_payload"
    if not posture.venue_posture:
        return None, "venue_posture_missing"
    return posture, None


def _extract_last_json_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    else:
        return payload if isinstance(payload, dict) else None

    decoder = json.JSONDecoder()
    candidate: dict[str, Any] | None = None
    for index, char in enumerate(str(text or "")):
        if char != "{":
            continue
        try:
            decoded, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and "venue_posture" in decoded:
            candidate = decoded
    return candidate


__all__ = ["build_global_posture_prompt", "parse_global_posture_completion"]
