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
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from . import decision_reason, llm, trade_plan
from .agent_context import INDICATOR_COLUMNS
from .features import DEFAULT_INDICATORS
from .indicator_watch import WATCH_VALID_OPERATORS
from .semantic.catalog import INDICATOR_LABEL_VALUES

Action = Literal["BUY", "SELL", "HOLD"]
Intent = Literal["OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "HOLD"]

# Modèle du brain runtime : Codex Spark 5.3 (famille rapide) à effort medium.
# Spark = faible latence, adapté à un agent en veille qui décide à chaque réveil.
DEFAULT_MODEL = llm.DEFAULT_SPARK_MODEL

_DECISION_KEYS = {"symbol", "action", "quantity", "confidence", "rationale", "decision_reason_code"}
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
    cancel_watch_ids: list[str] = field(default_factory=list)  # plans/veilles à annuler (correction)
    context_request: dict | None = None
    learning: str | None = None  # note runtime que l'agent veut retenir (boucle de feedback)
    domain_tools: dict | None = None  # traces tournée d'outils (runtime.tool_*)
    decision_reason_code: str = "UNKNOWN"
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


@dataclass(frozen=True)
class BatchToolCallRequest:
    """Le lot a répondu par une tournée d'outils au lieu de décisions finales.

    `calls` reste BRUT (list[dict]) : la validation vit dans trader.agent_tools,
    côté daemon — codex_client reste un transport sans dépendance domaine."""
    calls: list[dict]
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None


# Énumérations `|`-jointes pour les schémas JSON inline des contrats, DÉRIVÉES des
# sources de vérité (pas tapées à la main : une liste figée diverge en silence).
_WATCH_INDICATOR_ENUM = "|".join(DEFAULT_INDICATORS)
_WATCH_OPERATOR_ENUM = "|".join(WATCH_VALID_OPERATORS)
_REASON_CODE_ENUM = decision_reason.reason_code_enum_text()

_OUTPUT_CONTRACT = (
    "Réponds UNIQUEMENT par un objet JSON valide, sans texte autour, de la forme:\n"
    '{"symbol": "<SYM>", "action": "BUY|SELL|HOLD", "quantity": <number>, '
    '"confidence": <0..1>, "rationale": "<court>", '
    '"next_wake_in_minutes": <number|null>, '
    '"intent": "OPEN_LONG|OPEN_SHORT|REDUCE|CLOSE|REVERSE|HOLD", '
    '"exit_plan": <object|null>, '
    '"indicator_watch": <object|null>, '
    '"cancel_watch_ids": [<watch_id>, ...], '
    f'"decision_reason_code": "{_REASON_CODE_ENUM}", '
    '"learning": <string|null>}\n'
    "`learning` est optionnel : une note courte que tu veux retenir pour tes "
    "prochains réveils (ce que tu observes, ce que tu attends). Le daemon te la "
    "réinjecte via `context.learnings`. N'écris une note que si elle est utile.\n"
    "`next_wake_in_minutes` est optionnel : utilise-le seulement si ce symbole doit "
    "override la cadence globale par défaut. Pour une ouverture de position, fournis "
    "un `exit_plan` avec hard_stop, take_profits, trailing_stop, "
    "profit_protection et/ou exit_watch. max_hold_minutes est optionnel : "
    "n'en ajoute un que si la thèse a une expiration temporelle explicite ; "
    "sinon laisse-le absent/null. "
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
    '"cancel_watch_ids": [<watch_id>, ...], '
    f'"decision_reason_code": "{_REASON_CODE_ENUM}", '
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
    f'"conditions": [{{"symbol":"<SYM>","indicator":"{_WATCH_INDICATOR_ENUM}","op":"{_WATCH_OPERATOR_ENUM}",'
    '"value": <number>, "interval":"15m|30m|1h|4h|1d", "lookback":"5d|1mo|3mo|6mo|1y", "window": <number>, "as_of":"latest"}], '
    '"order": <object|null>}. '
    "Le daemon évaluera cette veille sans appel modèle jusqu'à expiration.\n"
    "Option B, seulement si un indicateur précis manque pour décider, demande un "
    "complément borné:\n"
    '{"symbol": "<SYM>", "action": "REQUEST_CONTEXT", "rationale": "<pourquoi>", '
    f'"requests": [{{"symbol": "<SYM>", "indicators": ["{_WATCH_INDICATOR_ENUM}"], '
    '"timeframe": "15m|30m|1h|4h|1d", "lookback": "5d|1mo|3mo|6mo|1y", '
    '"window": 48, "as_of": "latest"}]}\n'
    "Ne demande jamais de barres brutes. Demande peu d'indicateurs, sur peu de symboles. "
    "Si le marché est mort ou sans edge, décide HOLD avec un prochain réveil plus lent."
)


_DECISION_GUIDANCE = (
    "# Ton échelle d'engagement (du jugement immédiat au scénario délégué)\n"
    "À chaque réveil, choisis le bon outil — pas par défaut le premier :\n"
    "1. DÉCIDER maintenant (BUY/SELL) : l'edge est là, tout de suite.\n"
    "2. VEILLER (`indicator_watch` on_trigger=WAKE) : une question au marché — "
    "tu seras rappelé pour juger avec des données fraîches.\n"
    "3. ARMER un scénario (`on_trigger=EXECUTE_ORDER` + `order`) : une décision "
    "conditionnelle déjà prise — le daemon exécute au déclenchement sans te "
    "rappeler. Tu peux armer PLUSIEURS scénarios alternatifs sur un même symbole "
    "(cassure haute → long, cassure basse → short) : un seul se réalisera, les "
    "autres seront annulés (position existante) ou expireront.\n"
    "4. HOLD simple : le daemon te réveillera sur événement (trigger, régime de "
    "famille fort, signal) et te garantit une revue périodique — inutile de "
    "demander un réveil court « pour surveiller ».\n"
    "Un bon réveil produit des scénarios ; un réveil qui ne produit ni décision, "
    "ni veille, ni plan était probablement inutile.\n\n"
    "# Tes plans déjà en place\n"
    "`active_watches` (fourni par symbole) liste tes veilles et plans armés "
    "ACTIFS : id, kind, intent, conditions, expiration. Relis-les avant d'agir et "
    "corrige au lieu d'empiler. Pour abandonner un plan, mets son `id` dans "
    "`cancel_watch_ids`. Corriger un plan = l'annuler (`cancel_watch_ids`) ET "
    "reposer un `indicator_watch` à jour dans la même décision.\n\n"
    "# Semantic layer\n"
    "Les indicateurs fiables sont calculés par le code. Le prompt expose "
    "`context.cockpit` (compact, sans barres brutes). Si ce cockpit ne suffit pas, "
    "demande un complément borné via REQUEST_CONTEXT ; le daemon injectera "
    "`context.research` avec les indicateurs calculés, et `context.prior_rationale` "
    "(ta demande initiale) pour reprendre ton raisonnement sans repartir de zéro.\n\n"
    "# Plans de sortie\n"
    "Quand tu ouvres ou reverses une position, fournis un `exit_plan` structuré "
    "utile : hard_stop, take_profits, trailing_stop, profit_protection et/ou "
    "exit_watch. max_hold_minutes est optionnel : n'en ajoute un que si la "
    "thèse a une expiration temporelle explicite (catalyseur, fenêtre de "
    "réaction, ou setup qui doit marcher avant une échéance précise) ; sinon "
    "laisse-le absent/null. `profit_protection` est "
    "optionnel : utilise-le seulement si le setup justifie une sécurisation "
    "progressive; sinon le daemon ne l'ajoute pas de lui-même. `exit_watch` "
    "est une veille d'invalidation attachée au trade : si elle déclenche, "
    "le daemon te réveille avec le trigger; il ne ferme pas automatiquement. Le daemon "
    "appliquera ce plan mécaniquement.\n\n"
    "# Timers et veilles indicateurs\n"
    "Économie d'appels (D7) : le daemon te réveille DE LUI-MÊME sur événement "
    "(trigger de watch, position ouverte, régime de famille fort, signal cockpit) "
    "et te garantit une revue périodique de chaque symbole. Ne fixe un "
    "`next_wake_in_minutes` court que si ton plan l'exige vraiment ; sinon "
    "préfère poser une `indicator_watch` et laisser le réveil événementiel "
    "travailler — chaque réveil que tu demandes consomme un appel modèle.\n"
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
    "clôturés à tes décisions d'entrée : `realized_pnl` est NET de frais ; "
    "`realized_gross_pnl` et `total_commissions` te montrent combien les "
    "commissions ont mangé (un brut ~nul avec des frais positifs = sur-trading). "
    "Calibration par bucket de confidence (`by_confidence`) et coût par raison de "
    "sortie (`by_exit_reason`), chacun avec `total_gross_pnl`/`total_commission`. "
    "Si tes calls confiants perdent ou si une raison de sortie te coûte cher, "
    "ajuste ta thèse et écris-le dans `learning`. `context.learnings` te rappelle "
    "tes notes précédentes avec leur issue.\n\n"
    "# Frais de transaction\n"
    "Chaque trade paie une commission à l'entrée ET à la sortie. Le cockpit donne "
    "par symbole `be_ref_bps` (break-even aller-retour en points de base : "
    "mouvement minimal du prix pour couvrir les frais), `fee` (coût aller-retour) "
    "et `fee_ccy` (devise). ATTENTION : `be_ref_bps` est calculé pour un ordre de "
    "`cockpit.fee_ref_notional` ; un ordre PLUS PETIT coûte proportionnellement "
    "plus cher (minimum par ordre), donc ton break-even réel est plus haut que "
    "`be_ref_bps` si tu sizes plus petit. N'ouvre pas un scalp dont l'amplitude "
    "attendue est inférieure à `be_ref_bps` : un aller-retour neutre sur le prix "
    "est PERDANT une fois les frais payés. Privilégie les setups dont le mouvement "
    "espéré dépasse nettement le break-even.\n\n"
    "# Dimensionnement natif\n"
    "Chaque symbole porte sa devise `ccy`, `fx_usd` (USD par unité), "
    "`risk_budget_native` et `max_order_native` (déjà dans la devise du titre). "
    "TOUS ses prix, indicateurs, swings et niveaux sont dans `ccy`. Tes `hard_stop`, "
    "`take_profits` ET ta `quantity` sont dans cette MÊME devise (PAS en USD). "
    "Dimensionne en unités du titre : `quantity` ≈ `risk_budget_native` ÷ distance "
    "au hard_stop, puis relis `context.risk_capacity.per_symbol[SYM]` : "
    "`max_buy_qty` et `max_sell_qty` sont les plafonds de quantité réellement "
    "encore passables à ce réveil, après `max_order_value`, `max_position_value` "
    "et `max_gross_exposure`. Pour OPEN_LONG/BUY, ta `quantity` ne doit pas "
    "dépasser `max_buy_qty`; pour OPEN_SHORT/SELL, elle ne doit pas dépasser "
    "`max_sell_qty`. `gross_remaining_usd` et `max_*_notional_native` te donnent "
    "l'argent restant si tu veux raisonner en notionnel, mais la sortie attendue "
    "reste une quantité du titre. Si ton sizing théorique dépasse ce plafond, "
    "réduis-le toi-même ou HOLD au lieu de proposer un ordre que le daemon "
    "rejettera. Si `context.gross_budget_feedback` est présent, c'est qu'au "
    "dernier cycle des ouvertures ont été recalées faute de marge gross (partagée "
    "entre TOUS les symboles, servie par conviction décroissante) : sois plus "
    "sélectif et concentre la marge sur tes meilleures convictions. "
    "Le portefeuille (equity, cash) est en USD pour ta vue d'ensemble ; "
    "tu ne convertis JAMAIS, tout est déjà fourni dans la bonne devise. Le daemon "
    "borne ta `quantity` avec le fusible currency-correct avant exécution.\n\n"
    "# Régime cross-asset\n"
    "`context.regime_families` donne la synthèse directionnelle par famille "
    "thématique (momentum ~3 séances) : {dir, frac, up, down, n}. Quand frac >= 0.7 "
    "sur une ou plusieurs familles, un événement de régime (risk-on/off, souvent "
    "news/macro) est en cours : c'est un signal d'OPPORTUNITÉ directionnelle à "
    "juger — un symbole retardataire de la famille peut suivre le flux — pas un "
    "frein de plus. Pèse le risque associé (volatilité, retournement possible), "
    "mais ne l'ignore pas par défaut. `context.learnings.guardrails` liste les "
    "invariants humains ; le reste des learnings est un pattern appris, que tu "
    "peux remettre en question si les données le contredisent.\n\n"
    "# Contexte compact\n"
    "Le contexte ne contient PAS les barres brutes. Utilise le cockpit rapide pour "
    "décider, ou demande un petit complément d'indicateurs si c'est vraiment utile. "
    "Dans `context.cockpit`, `rs=force relative courte` sert au timing d'entrée, "
    "tandis que `rs_d=force relative daily` sert à vérifier la thèse swing et les "
    "divergences titre↔famille. "
    "Les requêtes d'indicateurs suivent le cube "
    "`symbol × indicators × timeframe × lookback × window × as_of`; `4h` est "
    "supporté comme timeframe sémantique.\n\n"
)


def build_prompt(*, mandate: str, memory: str, context: dict, allow_context_request: bool = False) -> str:
    """Assemble le prompt. Le COMPORTEMENT vit dans `mandate`/`memory` (boucle 1),
    pas en dur ici."""
    output_contract = _COMPACT_OUTPUT_CONTRACT if allow_context_request else _OUTPUT_CONTRACT
    return (
        "Tu es le PLANIFICATEUR d'un système de trading paper : tu conçois des "
        "scénarios que le daemon exécute mécaniquement ; tu n'opères pas le marché "
        "en continu.\n\n"
        f"# Mandat\n{mandate}\n\n"
        f"# Mémoire / stratégie\n{memory}\n\n"
        f"{_DECISION_GUIDANCE}"
        f"# Contexte marché et portefeuille (JSON)\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"# Contrat de sortie\n{output_contract}\n"
    )


_BATCH_FINAL_CONTRACT = (
    'Réponds UNIQUEMENT par {"decisions": [ <obj>, ... ]} avec EXACTEMENT une entrée '
    "par symbole listé.\n"
    'Chaque <obj>: {"symbol":"<SYM>","action":"BUY|SELL|HOLD","quantity":<number>,'
    '"confidence":<0..1>,"rationale":"<court>","next_wake_in_minutes":<number|null>,'
    '"intent":"OPEN_LONG|OPEN_SHORT|REDUCE|CLOSE|REVERSE|HOLD","exit_plan":<object|null>,'
    '"indicator_watch":<object|null>,"cancel_watch_ids":[<watch_id>,...],'
    f'"decision_reason_code":"{_REASON_CODE_ENUM}","learning":<string|null>}}\n'
    "Pour une ouverture, fournis un `exit_plan` conforme au schéma ci-dessous "
    "(hard_stop, take_profits, trailing_stop, profit_protection et/ou exit_watch). "
    "max_hold_minutes est optionnel : seulement si la thèse a une expiration "
    "temporelle explicite. `learning` optionnel : note "
    "à retenir, réinjectée via context.learnings. Si tu n'es pas sûr -> action=HOLD."
)

_BATCH_COMPACT_SUFFIX = (
    "\nPour un symbole précis où un indicateur manque, mets à la place "
    '{"symbol":"<SYM>","action":"REQUEST_CONTEXT","rationale":"<pourquoi>",'
    f'"requests":[{{"symbol":"<SYM>","indicators":["{_WATCH_INDICATOR_ENUM}"],"timeframe":"15m|30m|1h|4h|1d",'
    '"lookback":"5d|1mo|3mo|6mo|1y","window":48,"as_of":"latest"}]}. '
    "Le daemon résoudra puis redemandera la décision finale de CE symbole. "
    "Ne demande du contexte que si c'est vraiment utile."
)


def _exit_plan_contract() -> str:
    """Schéma exact accepté par `trade_plan.validate_exit_plan`, dérivé de sa
    source de vérité pour les `trail_type`."""
    trail_type_enum = "|".join(trade_plan.TRAILING_STOP_TRAIL_TYPES)
    return (
        "# Schéma exit_plan\n"
        "`exit_plan`: objet|null. Pour OPEN_LONG/OPEN_SHORT, utilise ces champs "
        "exacts:\n"
        '`hard_stop`: nombre > 0 OU objet {type:"price|percent|volatility_multiple|structural", ...}. '
        '`type:"price"` reste pass-through. Le hard_stop relatif est résolu '
        "mécaniquement au tir, sans rappel LLM; cela couvre "
        "percent/volatility_multiple/structural. Schéma relatif: "
        '{type:"percent", percent:<0..1>, min_pct?, max_pct?} ou '
        '{type:"volatility_multiple", multiple:<requis, >0>, min_pct?, max_pct?} ou '
        '{type:"structural", anchor:"swing_low|swing_high|vwap", '
        "window:<requis, >0>, buffer_pct?|buffer_atr?, min_pct?, max_pct?}.\n"
        "min_pct/max_pct sont des bornes de validation : si ton niveau résolu sort de "
        "ces bornes, l'ordre est rejeté. Le daemon ne déplace jamais le hard_stop "
        "pour le faire rentrer dans une borne.\n"
        "Pour un stop structural swing_low/swing_high en fenêtre 24 ou 48, le "
        "cockpit te donne la distance du swing BRUT (sl24/sl48 sous le prix, "
        "sh24/sh48 au-dessus, fraction du prix, mêmes barres que la résolution au "
        "tir) : centre min_pct/max_pct sur cette distance + ton buffer_pct/atr "
        "éventuel + une marge, au lieu de deviner. La validation porte sur la "
        "distance APRÈS buffer. (vwap ou autre fenêtre : pas de colonne, estime.)\n"
        "`take_profits`: liste d'OBJETS "
        "{price:<requis, >0>, fraction:<optionnel, >0>} "
        'OU {type:"risk_multiple", r:<requis, >0>, fraction?}. '
        "Les TP en R sont résolus au tir depuis la distance du hard_stop.\n"
        'Pour REVERSE, garde un hard_stop prix résolu: nombre > 0 OU {type:"price", price:<requis, >0>}.\n'
        f'`trailing_stop`: null OU {{trail_type:"{trail_type_enum}", '
        "trail_value:<requis, >0>}. "
        "`trail_type` doit être exactement l'un de cet enum. "
        "Unités trail_value: percent = fraction (0.004 = 0.4%); "
        "price = distance absolue en prix; volatility_multiple = multiple de la "
        "volatilité récente. Sans enabled_after, le trail "
        "ne s'arme qu'une fois en profit au moins égal au trail.\n"
    )


def _batch_final_contract() -> str:
    return f"{_BATCH_FINAL_CONTRACT}\n{_exit_plan_contract()}"


def _batch_compact_contract() -> str:
    return _batch_final_contract() + _BATCH_COMPACT_SUFFIX


def _indicator_watch_vocabulary() -> str:
    """Vocabulaire EXACT accepté par le validateur de watch, dérivé des sources de
    vérité (`features.DEFAULT_INDICATORS`, `indicator_watch.WATCH_VALID_OPERATORS`,
    `agent_context.INDICATOR_COLUMNS`, `semantic.catalog.INDICATOR_LABEL_VALUES`) —
    jamais recopié à la main pour ne pas diverger. But : l'agent emploie les noms
    canoniques (pas les abréviations du cockpit) et les opérateurs exacts, sinon la
    condition est rejetée."""
    indicators = " ".join(DEFAULT_INDICATORS)
    operators = " ".join(WATCH_VALID_OPERATORS)
    aliases = ", ".join(
        f"{abbrev}={canonical}" for canonical, abbrev in INDICATOR_COLUMNS.items()
    )

    # Mapping label -> float, dérivé de INDICATOR_LABEL_VALUES.
    # Quand plusieurs labels pointent vers la même valeur, la forme longue est
    # canonique ; la forme courte est présentée comme alias.
    label_lines: list[str] = []
    for indicator_name, mapping in INDICATOR_LABEL_VALUES.items():
        # Grouper par valeur float pour détecter les alias
        by_value: dict[float, list[str]] = {}
        for lbl, val in mapping.items():
            by_value.setdefault(val, []).append(lbl)
        parts: list[str] = []
        for val, lbls in sorted(by_value.items(), key=lambda kv: kv[0], reverse=True):
            # heuristique : la forme longue est la plus longue
            lbls_sorted = sorted(lbls, key=len, reverse=True)
            canonical_lbl = lbls_sorted[0]
            alias_lbls = lbls_sorted[1:]
            if alias_lbls:
                alias_str = ", ".join(f"{a} (alias)" for a in alias_lbls)
                parts.append(f"{canonical_lbl} -> {val} [{alias_str}]")
            else:
                parts.append(f"{canonical_lbl} -> {val}")
        label_lines.append(f"  {indicator_name}: {'; '.join(parts)}")
    label_mapping = "\n".join(label_lines)

    return (
        "# Vocabulaire des veilles (indicator_watch / exit_watch)\n"
        "Dans une condition de watch, `indicator` doit être l'un de ces noms "
        f"CANONIQUES exacts:\n{indicators}\n"
        f"`op` doit être l'un de: {operators}\n"
        "Le cockpit affiche des abréviations courtes ; dans une watch, emploie le "
        f"nom canonique correspondant: {aliases}.\n"
        "`value` doit être un nombre fini (float) : utilise l'équivalent numérique "
        "des valeurs nommées ci-dessous (ex: chart_breakout == 1.0 pour breakout_up, "
        "candlestick_signal == -0.5 pour shooting_star). Un label string connu est "
        "toléré (converti automatiquement) mais préfère le nombre ; un label inconnu "
        "est rejeté:\n"
        f"{label_mapping}\n"
        "Une watch minimale = `ttl_minutes` + une condition "
        '{"symbol","indicator","op","value","interval","window","as_of":"latest"}. '
        "Toute condition dont l'`indicator` ou l'`op` sort de ces listes est rejetée.\n"
        "# Plans armés (EXECUTE_ORDER)\n"
        "`on_trigger:\"EXECUTE_ORDER\"` arme un scénario d'entrée que le daemon "
        "exécutera au déclenchement SANS re-appel modèle : fournis `order` = "
        '{"intent":"OPEN_LONG|OPEN_SHORT","qty":<number>,"confidence":<0..1>,'
        '"exit_plan":{"hard_stop":{"type":"price|percent|volatility_multiple|structural",...},...},"rationale":"..."}. '
        "Contrat strict à l'armement : hard_stop peut être en prix OU relatif, "
        "hard_stop requis, qty>0, "
        "confidence explicite — sinon la watch est dégradée en WAKE_WITH_ORDER_INTENT "
        "(l'ordre repassera par toi). Schéma relatif armé : "
        '`hard_stop` {type:"percent", percent:<0..1>, min_pct?, max_pct?} ou '
        '{type:"volatility_multiple", multiple:<requis, >0>, min_pct?, max_pct?} ou '
        '{type:"structural", anchor:"swing_low|swing_high|vwap", '
        "window:<requis, >0>, buffer_pct?|buffer_atr?, min_pct?, max_pct?}; "
        '`take_profits[]` peut utiliser {type:"risk_multiple", r:<requis, >0>, fraction?}. '
        "Le hard_stop relatif est résolu en prix au déclenchement sur barres FRAÎCHES, puis les TP en R aussi — "
        "vrai pour percent, volatility_multiple ET structural, à égalité. "
        "min_pct/max_pct sont des bornes de validation : si ton niveau résolu sort de "
        "ces bornes, l'ordre est rejeté ; le daemon ne déplace jamais le hard_stop. "
        "Pour structural, window est en barres du timeframe runtime ; pour "
        "swing_low/high en fenêtre 24/48 le cockpit donne la distance du swing "
        "BRUT (sl24/sl48, sh24/sh48, fraction du prix) — centre min_pct/max_pct "
        "dessus + ton buffer éventuel + une marge (la validation porte sur la "
        "distance APRÈS buffer, et un stop ARMÉ est re-résolu sur barres fraîches "
        "au déclenchement). vwap ou autre fenêtre : pas de colonne, estime. "
        "Chaque forme exprime une logique d'invalidation : percent = distance fixe en %, "
        "volatility_multiple = distance proportionnelle à la volatilité récente, "
        "structural = niveau d'invalidation chartiste (sous le swing_low pour un long, "
        "au-dessus du swing_high pour un short, ou vwap). "
        "Choisis la forme qui colle à ton invalidation. Un "
        "prix absolu (type:price) n'est PAS recalibré au tir : figé à l'armement, il se "
        "décale si la volatilité bouge d'ici le déclenchement (ex. ouverture) — "
        "utilise-le seulement si c'est vraiment ton niveau d'invalidation. "
        "TTL max 4 h — cale-le sur ton scénario ; tu peux ré-armer à ta prochaine revue. Au "
        "déclenchement le daemon annule et te réveille si le prix a déjà franchi le "
        "stop ou si une position existe ; le gate de risque s'applique comme à tout "
        "ordre. C'est l'outil du planificateur : préfère un plan armé à un réveil "
        "court quand ton scénario est précis.\n\n"
    )


def build_batch_prompt(
    *,
    mandate: str,
    memory: str,
    shared_context: dict,
    symbols_payload: list[dict],
    allow_context_request: bool = False,
) -> str:
    """Prompt batch : contexte PARTAGÉ (cockpit/portefeuille/KPI/attribution/learnings)
    envoyé UNE fois, puis la liste des symboles à décider -> un seul appel modèle."""
    contract = _batch_compact_contract() if allow_context_request else _batch_final_contract()
    return (
        "Tu es le PLANIFICATEUR d'un système de trading paper : tu conçois des "
        "scénarios — entrées armées, veilles, plans de sortie — que le daemon "
        "exécute mécaniquement ; tu n'opères pas le marché en continu. Le contexte "
        "PARTAGÉ (cockpit de tout l'univers, portefeuille, KPI, attribution, "
        "learnings) est donné UNE fois ; rends une décision pour CHAQUE symbole "
        "de la liste.\n\n"
        f"# Mandat\n{mandate}\n\n"
        f"# Mémoire / stratégie\n{memory}\n\n"
        f"{_DECISION_GUIDANCE}"
        f"{_indicator_watch_vocabulary()}"
        f"# Contexte partagé (JSON)\n{json.dumps(shared_context, ensure_ascii=False)}\n\n"
        f"# Symboles à décider (JSON)\n{json.dumps(symbols_payload, ensure_ascii=False)}\n\n"
        f"# Contrat de sortie\n{contract}\n"
    )


def _extract_json(text: str) -> dict:
    """Récupère le 1er objet JSON du texte (Codex peut entourer de prose)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("aucun objet JSON trouvé")
    return json.loads(text[start : end + 1])


def _optional_float(data: dict, key: str) -> float | None:
    if data.get(key) is None:
        return None
    return float(data[key])


def _optional_upper_str(data: dict, key: str) -> str | None:
    if data.get(key) is None:
        return None
    return str(data[key]).upper()


def _optional_dict(data: dict, key: str) -> dict | None:
    if data.get(key) is None:
        return None
    return dict(data[key])


def _cancel_watch_ids(data: dict) -> list[str]:
    raw_ids = data.get("cancel_watch_ids")
    if not isinstance(raw_ids, list):
        return []
    return [str(wid) for wid in raw_ids if isinstance(wid, str)]


def _decision_reason_code(data: dict) -> str:
    if "decision_reason_code" not in data or data.get("decision_reason_code") is None:
        return decision_reason.infer_reason_code(data)
    return decision_reason.normalize_reason_code(data.get("decision_reason_code"))


def _decision_from_dict(data: dict, symbol: str) -> Decision:
    if not isinstance(data, dict):
        raise ValueError("élément non-objet")
    missing = _DECISION_KEYS - data.keys()
    blocking_missing = missing - {"decision_reason_code"}
    if blocking_missing:
        raise ValueError(f"clés manquantes: {blocking_missing}")
    action = str(data["action"]).upper()
    if action not in ("BUY", "SELL", "HOLD"):
        raise ValueError(f"action invalide: {action}")
    return Decision(
        symbol=str(data["symbol"]),
        action=action,  # type: ignore[arg-type]
        quantity=float(data["quantity"]),
        confidence=float(data["confidence"]),
        rationale=str(data["rationale"]),
        next_wake_in_minutes=_optional_float(data, "next_wake_in_minutes"),
        intent=_optional_upper_str(data, "intent"),  # type: ignore[arg-type]
        exit_plan=_optional_dict(data, "exit_plan"),
        indicator_watch=_optional_dict(data, "indicator_watch"),
        cancel_watch_ids=_cancel_watch_ids(data),
        learning=_normalize_learning(data.get("learning")),
        decision_reason_code=_decision_reason_code(data),
    )


def parse_decision(raw_text: str, symbol: str) -> Decision:
    return _decision_from_dict(_extract_json(raw_text), symbol)


def _indicator_requests_from(data: dict, symbol: str) -> list[IndicatorRequest]:
    raw_requests = data.get("requests") or data.get("indicator_requests") or []
    requests: list[IndicatorRequest] = []
    for item in raw_requests:
        if not isinstance(item, dict):
            continue
        indicators = item.get("indicators") or item.get("names") or item.get("indicator") or []
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
    return requests


def _response_from_dict(data: dict, symbol: str) -> Decision | ContextResearchRequest:
    if not isinstance(data, dict):
        raise ValueError("élément non-objet")
    action = str(data.get("action", "")).upper()
    if action in {"REQUEST_CONTEXT", "NEEDS_CONTEXT"} or data.get("needs_context") is True:
        return ContextResearchRequest(
            symbol=str(data.get("symbol") or symbol),
            rationale=str(data.get("rationale") or ""),
            requests=_indicator_requests_from(data, symbol),
            next_wake_in_minutes=_optional_float(data, "next_wake_in_minutes"),
        )
    return _decision_from_dict(data, symbol)


def parse_decision_or_context_request(raw_text: str, symbol: str) -> Decision | ContextResearchRequest:
    return _response_from_dict(_extract_json(raw_text), symbol)


def _extract_decisions_array(text: str) -> list:
    """Récupère la liste `decisions` d'un objet JSON batch (tolère la prose autour)."""
    data = _extract_json(text)
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("clé 'decisions' absente ou non-liste")
    return decisions


def _parse_batch_data(
    data: dict, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest]:
    """Corps de parse_batch sur un dict déjà extrait. Isolation per-élément."""
    by_symbol: dict[str, Decision | ContextResearchRequest] = {}
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}

    requested = set(symbols)
    for element in decisions:
        sym = str(element.get("symbol")) if isinstance(element, dict) else None
        if sym is None or sym not in requested:
            continue  # symbole hors périmètre ou élément non-objet -> ignoré
        try:
            if allow_context_request:
                by_symbol[sym] = _response_from_dict(element, sym)
            else:
                by_symbol[sym] = _decision_from_dict(element, sym)
        except Exception as e:  # noqa: BLE001 - isolation per-élément
            by_symbol[sym] = Decision.hold(sym, f"batch_bad_output: {e}")

    for sym in symbols:
        by_symbol.setdefault(sym, Decision.hold(sym, "missing_in_batch"))
    return by_symbol


def parse_batch(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest]:
    """Décode un tableau de décisions (une par symbole), avec ISOLATION per-élément :
    un élément invalide -> HOLD pour CE symbole, les autres passent. Un JSON global
    invalide -> tous HOLD. Tout symbole demandé mais absent de la réponse -> HOLD."""
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)


def parse_batch_or_tool_calls(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest:
    """Réponse batch OU tournée d'outils. tool_calls non vide prime ; toute
    malformation retombe sur le chemin décisions (fail-safe HOLD)."""
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}
    raw_calls = data.get("tool_calls")
    if isinstance(raw_calls, list):
        calls = [c for c in raw_calls if isinstance(c, dict)]
        if calls:
            return BatchToolCallRequest(calls=calls)
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)


def build_command(prompt: str, *, acpx_bin: str, model: str, timeout_s: int) -> list[str]:
    """Commande acpx pour une décision pure (codex, sans outils, sortie texte brute)."""
    return llm.build_acpx_command(prompt, acpx_bin=acpx_bin, model=model, timeout_s=timeout_s)


def _attach_llm_metadata(
    response: Decision | ContextResearchRequest | BatchToolCallRequest,
    completion: llm.LlmCompletion,
) -> Decision | ContextResearchRequest | BatchToolCallRequest:
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
    timeout_s: int = 900,
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


def decide_batch(
    *,
    symbols: list[str],
    mandate: str,
    memory: str,
    shared_context: dict,
    per_symbol: dict[str, dict],
    acpx_bin: str = "acpx",
    model: str = DEFAULT_MODEL,
    timeout_s: int = 900,
    allow_context_request: bool = False,
    llm_router: llm.LlmRouter | None = None,
) -> dict[str, Decision | ContextResearchRequest]:
    """UN seul appel modèle pour TOUS les symboles dus : le contexte partagé n'est
    envoyé qu'une fois (vs N fois en mode par-symbole). Isolation per-élément +
    tout échec -> HOLD. Retourne un dict symbole -> Decision|ContextResearchRequest."""
    if not symbols:
        return {}
    payload = [{"symbol": sym, **(per_symbol.get(sym) or {})} for sym in symbols]
    prompt = build_batch_prompt(
        mandate=mandate,
        memory=memory,
        shared_context=shared_context,
        symbols_payload=payload,
        allow_context_request=allow_context_request,
    )
    router = llm_router or llm.build_default_router_from_env(acpx_bin=acpx_bin, spark_model=model)
    completion = router.complete(prompt, timeout_s=timeout_s)
    if isinstance(completion, llm.LlmFailure):
        return {sym: _hold_from_llm_failure(sym, completion) for sym in symbols}
    results = parse_batch(completion.text, symbols, allow_context_request=allow_context_request)
    return {sym: _attach_llm_metadata(resp, completion) for sym, resp in results.items()}
