"""Override LLM de la rotation — surcharge tracée du default_hot.

Usage (prod) :
    le runtime injecte une fonction ``complete(prompt, timeout_s=...)`` concrète.

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
    max_candidates: int | None = None,
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
        max_candidates: borne de compatibilité explicite. ``None`` transmet la
                shortlist complète et constitue le comportement runtime.
    Returns:
        Prompt prêt à envoyer au LLM.
    """
    candidates = ranked if max_candidates is None else ranked[: max(0, max_candidates)]

    lines_candidates = "\n".join(
        _format_candidate_line(item)
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
        f"Shortlist candidate complète ({len(candidates)} symboles, radar + challengers) :\n"
        f"{lines_candidates}\n\n"
        "Ta mission : proposer des ajustements parcimonieux au hot-set.\n"
        "- Ajoute uniquement un symbole décorrélé ou nettement supérieur.\n"
        "- Retire uniquement un symbole redondant ou clairement dominé.\n"
        "- Si rien ne justifie de changement, retourne des listes vides.\n\n"
        'Réponds UNIQUEMENT en JSON, sans texte supplémentaire :\n'
        '{"add": [...], "remove": [...]}'
    )


def _format_candidate_line(item: dict[str, Any]) -> str:
    provenance = str(item.get("candidate_source") or item.get("provenance") or "radar")
    role = "challenger" if provenance != "radar" else "radar"
    line = (
        f"  - {item['symbol']} | attractiveness={item['attractiveness']} | "
        f"bias={item['bias']} | role={role} | provenance={provenance}"
    )
    fresh_news = item.get("fresh_news")
    if not isinstance(fresh_news, dict):
        return line

    event_types = ",".join(str(value) for value in fresh_news.get("event_types") or [])
    evidence = fresh_news.get("evidence") or []
    first_evidence = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
    headline = _compact_prompt_text(first_evidence.get("title"), limit=180)
    publisher = _compact_prompt_text(first_evidence.get("publisher"), limit=60)
    return (
        f"{line} | news_score={fresh_news.get('score', '?')} "
        f"| latest_news={fresh_news.get('latest_published_at', '?')} "
        f"| event_types={event_types or '?'} | headline={headline or '?'} "
        f"| publisher={publisher or '?'}"
    )


def _compact_prompt_text(value: Any, *, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


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
    max_candidates: int | None = None,
) -> Callable[[dict[str, Any]], dict[str, list[str]]]:
    """Fabrique une ``override_fn(payload)`` qui appelle le LLM injecté.

    Args:
        complete_fn: callable ``(prompt, *, timeout_s) -> str | object``.
                     En prod, brancher sur ``LlmRouter.complete``.
        timeout_s: délai transmis à ``complete_fn``.
        max_candidates: borne explicite de compatibilité ; ``None`` transmet
                toute la shortlist au prompt.
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
