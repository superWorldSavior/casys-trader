"""Override LLM de la rotation — surcharge tracée du default_hot.

Usage (prod) :
    from trader.agent.llm import LlmRouter
    override_fn = make_llm_override_fn(LlmRouter().complete)

Usage (test) :
    override_fn = make_llm_override_fn(lambda prompt, *, timeout_s: '{"add":[],"remove":[]}')
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable


def build_override_prompt(
    ranked: list[dict[str, Any]],
    default_hot: list[str],
    *,
    sticky: set[str] | frozenset[str] = frozenset(),
    market_context: dict[str, Any] | None = None,
    max_candidates: int = 50,
) -> str:
    """Construit un prompt FR demandant un override JSON du hot-set.

    Args:
        ranked: liste triée attractivité desc, chaque item contient
                ``symbol``, ``attractiveness``, ``bias``.
        default_hot: symboles retenus par la logique déterministe.
        sticky: symboles non-dégradables (positions + plans + watches) — ne peuvent
                pas être retirés même si l'agent les liste dans ``remove``.
        market_context: contexte de marché optionnel. v1 consomme uniquement la clé
                ``regime_families`` (dict famille → {dir, frac, ...}).
        max_candidates: nombre maximum de candidats listés dans le prompt.

    Returns:
        Prompt prêt à envoyer au LLM.
    """
    candidates = ranked[:max_candidates]

    lines_candidates = "\n".join(
        f"  - {item['symbol']} | attractiveness={item['attractiveness']} | bias={item['bias']}"
        for item in candidates
    )

    lines_default = ", ".join(default_hot) if default_hot else "(vide)"

    # Section sticky — anti-doublon : le LLM voit ce qu'il ne peut pas retirer
    sticky_section = ""
    if sticky:
        lines_sticky = ", ".join(sorted(sticky))
        sticky_section = (
            f"\nSymboles sticky (déjà surveillés/positionnés, non retirables) : {lines_sticky}\n"
            "Tu ne peux pas retirer ces symboles — ils sont protégés quelle que soit ta décision.\n"
            "Tiens-en compte pour ne pas dupliquer : ne les ajoute pas à nouveau.\n"
        )

    # Section contexte de marché — v1 : régime sectoriel uniquement
    market_section = ""
    if market_context:
        regime_families = market_context.get("regime_families")
        if regime_families:
            lines_regime = "\n".join(
                f"  - {family}: {info.get('dir', '?')} (frac={info.get('frac', '?')})"
                for family, info in sorted(regime_families.items())
            )
            market_section = (
                f"\nContexte de marché — régime sectoriel :\n{lines_regime}\n"
            )

    return (
        "Tu es un agent de rotation de portefeuille.\n\n"
        f"Hot-set par défaut (logique déterministe) : {lines_default}\n"
        f"{sticky_section}"
        f"{market_section}\n"
        f"Top {len(candidates)} candidats du radar :\n{lines_candidates}\n\n"
        "Ta mission : proposer des ajustements parcimonieux au hot-set.\n"
        "- Ajoute uniquement un symbole décorrélé ou nettement supérieur.\n"
        "- Retire uniquement un symbole redondant ou clairement dominé.\n"
        "- Si rien ne justifie de changement, retourne des listes vides.\n\n"
        'Réponds UNIQUEMENT en JSON, sans texte supplémentaire :\n'
        '{"add": [...], "remove": [...]}'
    )


def parse_override(text: str) -> dict[str, list[str]]:
    """Extrait le premier objet JSON de ``text`` et le valide.

    Tolère du texte libre autour et des blocs markdown.

    Returns:
        ``{"add": [...], "remove": [...]}`` — listes vides en cas d'échec.
    """
    _empty: dict[str, list[str]] = {"add": [], "remove": []}

    if not text:
        return _empty

    # Cherche le premier {...} dans le texte (potentiellement multi-lignes)
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not match:
        return _empty

    raw = match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty

    if not isinstance(data, dict):
        return _empty

    add = data.get("add", [])
    remove = data.get("remove", [])

    if not isinstance(add, list):
        add = []
    if not isinstance(remove, list):
        remove = []

    # Filtrer pour ne garder que des str
    add = [s for s in add if isinstance(s, str)]
    remove = [s for s in remove if isinstance(s, str)]

    return {"add": add, "remove": remove}


def make_llm_override_fn(
    complete_fn: Callable[..., Any],
    *,
    timeout_s: int = 120,
    max_candidates: int = 50,
) -> Callable[[dict[str, Any]], dict[str, list[str]]]:
    """Fabrique une ``override_fn(payload)`` qui appelle le LLM injecté.

    Args:
        complete_fn: callable ``(prompt, *, timeout_s) -> str | object``.
                     En prod, brancher sur ``LlmRouter.complete``.
        timeout_s: délai transmis à ``complete_fn``.
        max_candidates: transmis à ``build_override_prompt``.

    Returns:
        ``override_fn(payload)`` où ``payload = {"ranked": [...], "default_hot": [...]}``.
        Retourne ``{"add": [], "remove": []}`` en cas d'erreur (fail-safe).
    """
    _empty: dict[str, list[str]] = {"add": [], "remove": []}

    def override_fn(payload: dict[str, Any]) -> dict[str, list[str]]:
        ranked: list[dict] = payload.get("ranked", [])
        default_hot: list[str] = payload.get("default_hot", [])
        sticky: set[str] = payload.get("sticky") or frozenset()
        market_context: dict[str, Any] | None = payload.get("market_context")

        prompt = build_override_prompt(
            ranked,
            default_hot,
            sticky=sticky,
            market_context=market_context,
            max_candidates=max_candidates,
        )

        try:
            result = complete_fn(prompt, timeout_s=timeout_s)
        except Exception:
            return _empty

        # Extrait le texte — str directe ou attribut .text
        if result is None:
            return _empty
        if isinstance(result, str):
            text = result
        elif hasattr(result, "text"):
            text = result.text
        else:
            return _empty

        if not text:
            return _empty

        return parse_override(text)

    return override_fn
