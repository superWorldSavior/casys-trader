"""codex_client — appel programmatique à Codex (le brain décideur).

Invoque Codex en headless via `acpx --format quiet exec` (codex est l'agent par
défaut d'acpx). On lui passe le contexte + le mandat, et on force une sortie JSON
structurée (le schéma de décision). Aucune stratégie ici : juste le transport vers
le LLM et la validation stricte de sa réponse.

Décision PURE : on coupe les outils (`--allowed-tools ""`) et le terminal
(`--no-terminal`) — le brain ne fait que raisonner sur le contexte fourni, il ne
touche ni au FS ni au shell. `exec` = session jetable (pas d'état partagé) ->
isolation/idempotence : aucun appel ne contamine le suivant (un LLM échantillonne,
ce n'est donc pas du déterminisme bit-à-bit). L'état évolutif de l'agent est
externalisé — `memory.md` (boucle 1, humain) et `state/learnings.jsonl` (boucle 2,
machine) — et repassé dans le contexte à chaque réveil.

Fail-safe : toute erreur (timeout, binaire absent, JSON invalide) -> décision
HOLD. On ne trade JAMAIS sur une réponse douteuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from . import llm

Action = Literal["BUY", "SELL", "HOLD"]
Intent = Literal["OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "HOLD"]

# Modèle du brain runtime : Codex Spark 5.3 (famille rapide) à effort medium.
# Spark = faible latence, adapté à un agent en veille qui décide à chaque réveil.
DEFAULT_MODEL = llm.DEFAULT_SPARK_MODEL

_DECISION_KEYS = {"symbol", "action", "quantity", "confidence", "rationale"}
MAX_LEARNING_CHARS = 1000  # borne la note pour ne pas faire exploser le prompt/store


def _normalize_learning(value: object) -> str | None:
    """Accepte seulement une note textuelle non vide, bornée. Sinon None."""
    if not isinstance(value, str):
        return None
    note = value.strip()
    if not note:
        return None
    return note[:MAX_LEARNING_CHARS]


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: Action
    quantity: float          # nombre d'unités ; ignoré si HOLD
    confidence: float        # 0..1
    rationale: str
    next_wake_in_minutes: float | None = None  # override du timer pour CE symbole
    intent: Intent | None = None
    exit_plan: dict[str, Any] | None = None
    indicator_watch: dict[str, Any] | None = None
    learning: str | None = None  # note runtime que l'agent veut retenir (boucle de feedback)
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None
    llm_error: str | None = None

    @staticmethod
    def hold(symbol: str, reason: str) -> "Decision":
        return Decision(symbol=symbol, action="HOLD", quantity=0.0, confidence=0.0, rationale=reason, intent="HOLD")


@dataclass(frozen=True)
class IndicatorRequest:
    symbol: str
    indicators: list[str]
    window: int = 48
    timeframe: str = "1h"
    lookback: str | None = None
    as_of: str = "latest"


@dataclass(frozen=True)
class ContextResearchRequest:
    symbol: str
    rationale: str
    requests: list[IndicatorRequest]
    next_wake_in_minutes: float | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None


_OUTPUT_CONTRACT = (
    "Réponds UNIQUEMENT par un objet JSON valide, sans texte autour, de la forme:\n"
    '{"symbol": "<SYM>", "action": "BUY|SELL|HOLD", "quantity": <number>, '
    '"confidence": <0..1>, "rationale": "<court>", '
    '"next_wake_in_minutes": <number|null>, '
    '"intent": "OPEN_LONG|OPEN_SHORT|REDUCE|CLOSE|REVERSE|HOLD", '
    '"exit_plan": <object|null>, '
    '"indicator_watch": <object|null>, '
    '"learning": <string|null>}\n'
    "`learning` est optionnel : une note courte que tu veux retenir pour tes "
    "prochains réveils (ce que tu observes, ce que tu attends). Le daemon te la "
    "réinjecte via `context.learnings`. N'écris une note que si elle est utile.\n"
    "`next_wake_in_minutes` est optionnel : utilise-le seulement si ce symbole doit "
    "override la cadence globale par défaut. Pour une ouverture de position, fournis "
    "un `exit_plan` avec hard_stop, take_profits, trailing_stop, "
    "profit_protection, exit_watch et/ou max_hold_minutes. "
    "`indicator_watch` peut définir une veille conditionnelle avec `timeframe`, "
    "`lookback`, `window` et `as_of=latest` si tu veux être réveillé par signaux. "
    "Si tu n'es pas sûr, renvoie action=HOLD."
)

_COMPACT_OUTPUT_CONTRACT = (
    "Réponds UNIQUEMENT par un objet JSON valide, sans texte autour.\n"
    "Option A, si tu peux décider avec le cockpit compact, utilise le contrat final:\n"
    '{"symbol": "<SYM>", "action": "BUY|SELL|HOLD", "quantity": <number>, '
    '"confidence": <0..1>, "rationale": "<court>", '
    '"next_wake_in_minutes": <number|null>, '
    '"intent": "OPEN_LONG|OPEN_SHORT|REDUCE|CLOSE|REVERSE|HOLD", '
    '"exit_plan": <object|null>, '
    '"indicator_watch": <object|null>, '
    '"learning": <string|null>}\n'
    "`learning`: optionnel, note courte à retenir pour tes prochains réveils ; "
    "le daemon te la réinjecte via `context.learnings`.\n"
    "`exit_plan`: hard_stop nombre ou {price}; take_profits liste d'objets "
    "{price,fraction}; trailing_stop null ou {trail_type,trail_value}; "
    "profit_protection optionnel {arm_at_r, trigger_on_giveback_pct, "
    "close_fraction, move_stop_to, min_hold_minutes} si tu veux sécuriser "
    "progressivement un trade gagnant sans attendre TP1; exit_watch optionnel "
    "{ttl_minutes,cooldown_minutes,logic,conditions:[{indicator,op,value,timeframe,lookback,window}]} "
    "si tu veux être réveillé quand des indicateurs invalident la thèse de sortie.\n"
    "`indicator_watch`: optionnel. Utilise-le si le bon prochain réveil dépend "
    "d'une combinaison d'indicateurs plutôt que d'un simple timer. Format: "
    '{"ttl_minutes": <number>, "logic": "all|any", "on_trigger": "WAKE|WAKE_WITH_ORDER_INTENT", '
    '"conditions": [{"symbol":"<SYM>","indicator":"z_score|return|volatility|ohlc_volatility|efficiency_ratio|autocorrelation|candlestick_signal|chart_breakout|trend_slope|range_position","op":">|>=|<|<=|abs>|abs>=",'
    '"value": <number>, "interval":"15m|30m|1h|4h|1d", "lookback":"5d|1mo|3mo|6mo|1y", "window": <number>, "as_of":"latest"}], '
    '"order": <object|null>}. '
    "Le daemon évaluera cette veille sans appel modèle jusqu'à expiration.\n"
    "Option B, seulement si un indicateur précis manque pour décider, demande un "
    "complément borné:\n"
    '{"symbol": "<SYM>", "action": "REQUEST_CONTEXT", "rationale": "<pourquoi>", '
    '"requests": [{"symbol": "<SYM>", "indicators": ["z_score"], '
    '"timeframe": "15m|30m|1h|4h|1d", "lookback": "5d|1mo|3mo|6mo|1y", '
    '"window": 48, "as_of": "latest"}]}\n'
    "Ne demande jamais de barres brutes. Demande peu d'indicateurs, sur peu de symboles. "
    "Si le marché est mort ou sans edge, décide HOLD avec un prochain réveil plus lent."
)


def build_prompt(*, mandate: str, memory: str, context: dict, allow_context_request: bool = False) -> str:
    """Assemble le prompt. Le COMPORTEMENT vit dans `mandate`/`memory` (boucle 1),
    pas en dur ici."""
    output_contract = _COMPACT_OUTPUT_CONTRACT if allow_context_request else _OUTPUT_CONTRACT
    return (
        "Tu es l'agent décideur d'un système de trading paper.\n\n"
        f"# Mandat\n{mandate}\n\n"
        f"# Mémoire / stratégie\n{memory}\n\n"
        "# Semantic layer\n"
        "Les indicateurs fiables sont calculés par le code. Le prompt initial expose "
        "`context.cockpit` (compact, sans barres brutes). Si ce cockpit ne suffit pas, "
        "demande un complément borné via REQUEST_CONTEXT ; le daemon injectera "
        "`context.research` avec les indicateurs calculés, et `context.prior_rationale` "
        "(ta demande initiale) pour reprendre ton raisonnement sans repartir de zéro.\n\n"
        "# Plans de sortie\n"
        "Quand tu ouvres ou reverses une position, fournis autant que possible un "
        "`exit_plan` structuré : hard_stop, take_profits, trailing_stop, "
        "profit_protection, exit_watch et/ou max_hold_minutes. `profit_protection` est "
        "optionnel : utilise-le seulement si le setup justifie une sécurisation "
        "progressive; sinon le daemon ne l'ajoute pas de lui-même. `exit_watch` "
        "est une veille d'invalidation attachée au trade : si elle déclenche, "
        "le daemon te réveille avec le trigger; il ne ferme pas automatiquement. Le daemon "
        "appliquera ce plan mécaniquement.\n\n"
        "# Timers et veilles indicateurs\n"
        "Pour chaque symbole tu peux soit fixer `next_wake_in_minutes`, soit poser "
        "une `indicator_watch` temporaire multi-timeframe. Une watch est évaluée "
        "par le daemon jusqu'à `ttl_minutes`; si elle déclenche, le symbole est "
        "réveillé avec le trigger dans le contexte. Utilise "
        "`on_trigger=WAKE_WITH_ORDER_INTENT` seulement quand la condition décrit "
        "déjà précisément l'action prévue; l'ordre repassera par Codex et les "
        "garde-fous runtime.\n\n"
        "# Performance & auto-évaluation\n"
        "Pilote-toi avec tes propres résultats. `context.kpis` donne les KPI live "
        "(rendement, drawdown, sharpe…). `context.attribution` relie tes trades "
        "clôturés à tes décisions d'entrée : P&L réalisé, calibration par bucket de "
        "confidence (`by_confidence`) et coût par raison de sortie (`by_exit_reason`). "
        "Si tes calls confiants perdent ou si une raison de sortie te coûte cher, "
        "ajuste ta thèse et écris-le dans `learning`. `context.learnings` te rappelle "
        "tes notes précédentes avec leur issue.\n\n"
        "# Contexte compact\n"
        "Le contexte initial ne contient PAS les barres brutes. Utilise le cockpit "
        "rapide pour décider, ou demande un petit complément d'indicateurs si c'est "
        "vraiment utile. Les requêtes d'indicateurs suivent le cube "
        "`symbol × indicator × timeframe × lookback × window × as_of`; `4h` est "
        "supporté comme timeframe sémantique.\n\n"
        f"# Contexte marché et portefeuille (JSON)\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"# Contrat de sortie\n{output_contract}\n"
    )


def _extract_json(text: str) -> dict:
    """Récupère le 1er objet JSON du texte (Codex peut entourer de prose)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("aucun objet JSON trouvé")
    return json.loads(text[start : end + 1])


def parse_decision(raw_text: str, symbol: str) -> Decision:
    data = _extract_json(raw_text)
    missing = _DECISION_KEYS - data.keys()
    if missing:
        raise ValueError(f"clés manquantes: {missing}")
    action = str(data["action"]).upper()
    if action not in ("BUY", "SELL", "HOLD"):
        raise ValueError(f"action invalide: {action}")
    return Decision(
        symbol=str(data["symbol"]),
        action=action,  # type: ignore[arg-type]
        quantity=float(data["quantity"]),
        confidence=float(data["confidence"]),
        rationale=str(data["rationale"]),
        next_wake_in_minutes=(
            None
            if data.get("next_wake_in_minutes") is None
            else float(data["next_wake_in_minutes"])
        ),
        intent=(
            None
            if data.get("intent") is None
            else str(data["intent"]).upper()  # type: ignore[arg-type]
        ),
        exit_plan=(
            None
            if data.get("exit_plan") is None
            else dict(data["exit_plan"])
        ),
        indicator_watch=(
            None
            if data.get("indicator_watch") is None
            else dict(data["indicator_watch"])
        ),
        learning=_normalize_learning(data.get("learning")),
    )


def parse_decision_or_context_request(raw_text: str, symbol: str) -> Decision | ContextResearchRequest:
    data = _extract_json(raw_text)
    action = str(data.get("action", "")).upper()
    if action in {"REQUEST_CONTEXT", "NEEDS_CONTEXT"} or data.get("needs_context") is True:
        raw_requests = data.get("requests") or data.get("indicator_requests") or []
        requests: list[IndicatorRequest] = []
        for item in raw_requests:
            if not isinstance(item, dict):
                continue
            indicators = item.get("indicators") or item.get("names") or []
            if isinstance(indicators, str):
                indicators = [indicators]
            requests.append(
                IndicatorRequest(
                    symbol=str(item.get("symbol") or symbol),
                    indicators=[str(name) for name in indicators],
                    timeframe=str(item.get("timeframe") or item.get("interval") or "1h"),
                    lookback=(
                        None
                        if item.get("lookback") is None
                        else str(item["lookback"])
                    ),
                    window=int(item.get("window") or 48),
                    as_of=str(item.get("as_of") or "latest"),
                )
            )
        return ContextResearchRequest(
            symbol=str(data.get("symbol") or symbol),
            rationale=str(data.get("rationale") or ""),
            requests=requests,
            next_wake_in_minutes=(
                None
                if data.get("next_wake_in_minutes") is None
                else float(data["next_wake_in_minutes"])
            ),
        )
    return parse_decision(raw_text, symbol)


def build_command(prompt: str, *, acpx_bin: str, model: str, timeout_s: int) -> list[str]:
    """Commande acpx pour une décision pure (codex, sans outils, sortie texte brute)."""
    return llm.build_acpx_command(prompt, acpx_bin=acpx_bin, model=model, timeout_s=timeout_s)


def _attach_llm_metadata(
    response: Decision | ContextResearchRequest,
    completion: llm.LlmCompletion,
) -> Decision | ContextResearchRequest:
    return replace(
        response,
        llm_provider=completion.provider,
        llm_model=completion.model,
        llm_fallback_reason=completion.fallback_reason,
    )


def _hold_from_llm_failure(symbol: str, failure: llm.LlmFailure) -> Decision:
    return replace(
        Decision.hold(symbol, f"llm_failed:{failure.provider}:{failure.code}:{failure.message[:160]}"),
        llm_provider=failure.provider,
        llm_model=failure.model,
        llm_fallback_reason=failure.fallback_reason,
        llm_error=failure.code,
    )


def decide(
    *,
    symbol: str,
    mandate: str,
    memory: str,
    context: dict,
    acpx_bin: str = "acpx",
    model: str = DEFAULT_MODEL,
    timeout_s: int = 120,
    allow_context_request: bool = False,
    llm_router: llm.LlmRouter | None = None,
) -> Decision | ContextResearchRequest:
    """Appelle Codex (via acpx) et renvoie une Decision validée. Tout échec -> HOLD."""
    prompt = build_prompt(
        mandate=mandate,
        memory=memory,
        context=context,
        allow_context_request=allow_context_request,
    )
    router = llm_router or llm.build_default_router_from_env(acpx_bin=acpx_bin, spark_model=model)
    completion = router.complete(prompt, timeout_s=timeout_s)
    if isinstance(completion, llm.LlmFailure):
        return _hold_from_llm_failure(symbol, completion)

    try:
        if allow_context_request:
            response = parse_decision_or_context_request(completion.text, symbol)
        else:
            response = parse_decision(completion.text, symbol)
        return _attach_llm_metadata(response, completion)
    except Exception as e:  # noqa: BLE001
        return replace(
            Decision.hold(symbol, f"codex_bad_output: {e}"),
            llm_provider=completion.provider,
            llm_model=completion.model,
            llm_fallback_reason=completion.fallback_reason,
            llm_error="bad_output",
        )
