"""Prompt builders and output contracts for the trading agent."""

from __future__ import annotations

import json

from trader.market.features import DEFAULT_INDICATORS
from trader.planning import trade_plan
from trader.planning.indicator_watch import WATCH_VALID_OPERATORS
from trader.domain import decision_reason
from trader.domain.semantic.catalog import INDICATOR_COLUMNS, INDICATOR_LABEL_VALUES

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


def _decision_guidance(*, allow_context_request: bool) -> str:
    if allow_context_request:
        return _DECISION_GUIDANCE
    return _DECISION_GUIDANCE.replace(
        "# Semantic layer\n"
        "Les indicateurs fiables sont calculés par le code. Le prompt expose "
        "`context.cockpit` (compact, sans barres brutes). Si ce cockpit ne suffit pas, "
        "demande un complément borné via REQUEST_CONTEXT ; le daemon injectera "
        "`context.research` avec les indicateurs calculés, et `context.prior_rationale` "
        "(ta demande initiale) pour reprendre ton raisonnement sans repartir de zéro.\n\n",
        "# Semantic layer\n"
        "Les indicateurs fiables sont calculés par le code. Le prompt expose "
        "`context.cockpit` (compact, sans barres brutes). Si ce cockpit ne suffit pas, "
        "utilise une tournée `tool_calls` avec get_indicator_context ; le daemon injectera "
        "les résultats via `tool_results` pour reprendre ton raisonnement sans repartir de zéro.\n\n",
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
        f"{_decision_guidance(allow_context_request=allow_context_request)}"
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

_SYMBOL_CALLS_FINAL_CONTRACT = (
    'Le contrat final: {"decisions": [ <obj>, ... ]} avec EXACTEMENT une entrée '
    "par symbole listé.\n"
    'Chaque <obj>: {"symbol":"<SYM>","confidence":<0..1>,"rationale":"<court>",'
    f'"decision_reason_code":"{_REASON_CODE_ENUM}","calls":[<tool_call>,...]}}\n'
    "`calls: []` signifie HOLD explicite pour ce symbole. Ne mélange pas `calls` "
    "avec les anciens champs métier.\n"
    "Action tools finaux autorisés par symbole:\n"
    "- propose_order{intent:OPEN_LONG|OPEN_SHORT|REDUCE|CLOSE|REVERSE|ADD, qty?, risk_pct?, fraction?, exit?, thesis?} : "
    "propose un ordre; le daemon reste seul exécuteur et applique execution/exit/RiskGate. "
    "OPEN_LONG→BUY, OPEN_SHORT→SELL dérivent la side automatiquement. "
    "CLOSE/REDUCE/REVERSE/ADD dérivent aussi la side depuis ta position au portefeuille : "
    "inutile de fournir side/action pour ces intents; si tu la fournis, elle est ignorée "
    "et la side est quand même dérivée de la position. "
    "CLOSE ferme toute la position (omets qty) ; "
    "REDUCE réduit de fraction (ex: fraction:0.5) OU d'une qty absolue ; "
    "REVERSE dérive la side mais qty de la nouvelle jambe reste requise. "
    "ADD renforce la position dans son sens (long→BUY, short→SELL), qty requise ; "
    "fail-safe : ADD sans position ouverte → rejeté (add_without_position). "
    "Sizing : pour OPEN_LONG/OPEN_SHORT, au lieu de calculer qty toi-même, tu peux "
    "fournir risk_pct (ex: 0.005 = 0.5 % de l'equity) et omettre qty : le daemon "
    "dérive qty = risk_pct × equity / (distance_stop × fx_rate). "
    "Hors OPEN_LONG/OPEN_SHORT, risk_pct est ignoré et la qty/fraction requise garde la main. "
    "Requiert un hard_stop dans exit (OBLIGATOIRE sans quoi l'ordre est rejeté). "
    "Le gate max_risk_per_trade_pct reste le fusible : si risk_pct > limite, l'ordre est rejeté. "
    "qty explicite prime toujours sur risk_pct (Explicit Over Implicit). "
    '`thesis` est OPTIONNEL : {setup:"<setup court>", horizon:"intraday|swing|position", '
    "invalidation:\"<condition d'invalidation>\"} — tag structuré persisté pour l'attribution "
    "et la boucle d'apprentissage. N'en ajoute un que si la thèse est claire.\n"
    "- amend_exit{hard_stop?, tp?, trail?, protect?} : patche le plan de sortie du symbole "
    "déjà ouvert (remonte le stop au break-even, déplace un TP, reserre le trailing). "
    "Même vocabulaire compact que propose_order.exit (stop/tp/trail/protect). "
    "No-op tracé si pas de plan ouvert. "
    "Utilise-le sans propose_order sur ce symbole ; amend_exit + propose_order est rejeté. "
    "`calls:[{amend_exit}]` = HOLD + ajustement de gestion actif.\n"
    '- set_next_wake{minutes} OU {on:"session_open"|"macro_event"|"pre_earnings"} OU {when:<condition>, ttl_minutes?} : '
    "planifie la prochaine RECONSULTATION du symbole (l'agent reprend la main pour redécider). "
    "minutes = timer fixe (ex: 15 après une entrée) ; "
    "on:session_open = prochaine ouverture de séance de la place du symbole ; "
    "on:macro_event = avant le prochain événement macro (FOMC/CPI) ; "
    "on:pre_earnings = avant les prochains earnings (si la donnée existe) ; "
    "when = réveil-sur-indicateur : RECONSULTATION quand une condition indicateur devient vraie — "
    "équivalent interne à propose_indicator_watch{WAKE} mais dans set_next_wake. "
    'Format when: {"indicator":"<nom>","op":">=|>|<=|<|==","value":<float>,'
    '"interval":"15m|1h|4h","window":<int>,"as_of":"latest"} (même vocabulaire que les conditions de veille). '
    "ttl_minutes optionnel (défaut 60). "
    "INTERDIT de combiner when et propose_indicator_watch dans la même décision (conflit → HOLD). "
    "Distinction clé : set_next_wake = RECONSULTATION (l'agent re-juge) ; "
    "propose_indicator_watch = PLAN ARMÉ (exécution auto sans te reconsulter).\n"
    "- propose_indicator_watch{...} : pose une veille/plan armé avec le vocabulaire des veilles.\n"
    "- cancel_watch{id|ids|watch_ids} : annule uniquement tes veilles du symbole.\n"
    "- record_learning{note} : note courte bornée pour la mémoire runtime.\n"
    "Vocabulaire compact de `propose_order.exit`: "
    "stop -> hard_stop; tp[{r,fraction}|{price,fraction}] -> take_profits; "
    "trail{type,value}; protect{arm_r,giveback,close_fraction?,lock_r?,min_hold_minutes?}; "
    "exit_watch; max_hold_minutes. "
    "`protect.lock_r` verrouille le stop à +N R (0 = breakeven); "
    "aliases acceptés: after_r/enabled_after_r/activate_after_r -> arm_r, "
    "protect_r/lock_in_r -> lock_r.\n"
    "Exemple compact: "
    '{"symbol":"DASH","confidence":0.74,"rationale":"breakout propre",'
    f'"decision_reason_code":"ENTRY_SIGNAL","calls":[{{"tool":"propose_order","args":'
    '{"intent":"OPEN_LONG","qty":20,"exit":{"stop":{"struct":"swing_low","window":24},'
    '"tp":[{"r":1.4,"fraction":0.5}],"protect":{"arm_r":1.0,"giveback":0.35,"lock_r":0.25}}}}},'
    '{"tool":"set_next_wake","args":{"minutes":15}}]}'
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


def _round_label(max_rounds: int) -> str:
    rounds = max(int(max_rounds), 1)
    return "une seule tournée" if rounds == 1 else f"jusqu'à {rounds} tournées"


def _symbol_calls_final_contract(allow_tool_calls: bool = False, max_rounds: int = 1) -> str:
    """Contrat de sortie « action tools par symbole ».

    Au 1er passage (`allow_tool_calls`), l'agent garde le choix : émettre d'abord
    une tournée d'outils lecture-seule, OU rendre directement le contrat final.
    On ne bride pas ce choix (AX : faire confiance à l'agent). Au tour final,
    plus de tournée — la réponse `decisions` est imposée.
    """
    if allow_tool_calls:
        if max(int(max_rounds), 1) == 1:
            head = (
                "Au PREMIER tour, tu choisis librement : soit tu émets d'abord une "
                "tournée d'outils lecture-seule "
                '{"tool_calls":[...]}'
                " (cf. « Outils domaine » ci-dessus) pour aller chercher le contexte "
                "qui te manque, soit tu rends directement le contrat final ci-dessous. "
                "Après une tournée, tu rendras le contrat final.\n"
            )
        else:
            head = (
                f"À chaque tour ({_round_label(max_rounds)}), tu choisis librement : soit tu demandes "
                "des outils lecture-seule "
                '{"tool_calls":[...]}'
                " (cf. « Outils domaine » ci-dessus) pour aller chercher le contexte "
                "qui te manque, soit tu rends directement le contrat final ci-dessous. "
                "Au tour final imposé, plus aucun outil n'est accepté.\n"
            )
    else:
        head = (
            'Réponds UNIQUEMENT par {"decisions":[...]} ci-dessous'
            " : c'est le tour final, plus aucune tournée d'outils n'est acceptée.\n"
        )
    return head + _SYMBOL_CALLS_FINAL_CONTRACT


def _indicator_watch_vocabulary() -> str:
    """Vocabulaire EXACT accepté par le validateur de watch, dérivé des sources de
    vérité (`features.DEFAULT_INDICATORS`, `indicator_watch.WATCH_VALID_OPERATORS`,
    `semantic.catalog.INDICATOR_COLUMNS`, `semantic.catalog.INDICATOR_LABEL_VALUES`) —
    jamais recopié à la main pour ne pas diverger. But : l'agent emploie les noms
    canoniques (pas les abréviations du cockpit) et les opérateurs exacts, sinon la
    condition est rejetée."""
    indicators = " ".join(DEFAULT_INDICATORS)
    operators = " ".join(WATCH_VALID_OPERATORS)
    aliases = ", ".join(f"{abbrev}={canonical}" for canonical, abbrev in INDICATOR_COLUMNS.items())

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
        '`on_trigger:"EXECUTE_ORDER"` arme un scénario d\'entrée que le daemon '
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


_TOOL_CATALOG = (
    "# Outils domaine (OPTIONNELS — une seule tournée)\n"
    "Si le cockpit suffit, rends directement le contrat final. Sinon tu peux demander\n"
    "UNE tournée d'outils lecture-seule en répondant À LA PLACE du contrat final :\n"
    '{"tool_calls": [{"id": "c1", "tool": "<nom>", "args": {"symbols": ["2330.TW"]}}]}\n'
    "Bornes : 3 appels max par symbole, 24 par lot. Outils :\n"
    "- get_freshness{symbols:[…]} : exécution/planification/âge des données par symbole\n"
    "- get_active_plans{symbol?,limit?} : veilles et plans armés actifs (corrige au lieu d'empiler)\n"
    "- get_attribution{scope:summary|confidence|exit_reason|symbol, symbol?} : perf attribuée compacte\n"
    "- describe_data{} : cube sémantique (timeframes/lookbacks valides, windows, indicateurs)\n"
    "- find_indicators{concept} : cherche des indicateurs par concept (momentum, volatilité, …)\n"
    "- get_indicator_context{symbol,indicators:[…],timeframe?,lookback?,window?,as_of?} : cube indicateurs borné\n"
    "- recall_learnings{symbol?|family?|query?, limit?} : mémoire vérifiée — notes passées pondérées par leurs résultats réels\n"
    "Après la tournée tu recevras `tool_results` par symbole et tu DEVRAS rendre le contrat final\n"
    "(toute nouvelle tournée sera bloquée en HOLD).\n"
    "NB : get_indicator_context est la voie moderne de REQUEST_CONTEXT (les deux marchent) —\n"
    "préfère la tournée d'outils, qui te donne AUSSI plans/risque/attribution/mémoire en un tour.\n\n"
)


def _tool_catalog(
    *,
    allow_context_request: bool,
    max_tool_calls_per_symbol: int = 3,
    max_rounds: int = 1,
) -> str:
    rounds = max(int(max_rounds), 1)
    catalog = _TOOL_CATALOG.replace(
        "Bornes : 3 appels max par symbole, 24 par lot.",
        f"Bornes : {max_tool_calls_per_symbol} appels max par symbole, 24 par lot.",
    )
    if rounds > 1:
        catalog = catalog.replace(
            "# Outils domaine (OPTIONNELS — une seule tournée)",
            f"# Outils domaine (OPTIONNELS — {_round_label(rounds)})",
        ).replace(
            "UNE tournée d'outils lecture-seule en répondant À LA PLACE du contrat final :",
            f"{_round_label(rounds)} d'outils lecture-seule avant de rendre le contrat final :",
        ).replace(
            "Après la tournée tu recevras `tool_results` par symbole et tu DEVRAS rendre le contrat final\n"
            "(toute nouvelle tournée sera bloquée en HOLD).\n",
            "Après chaque tournée tu recevras `tool_results` par symbole. Au tour final, plus aucune tournée n'est acceptée.\n",
        )
    if allow_context_request:
        return catalog
    return catalog.replace(
        "NB : get_indicator_context est la voie moderne de REQUEST_CONTEXT (les deux marchent) —\n"
        "préfère la tournée d'outils, qui te donne AUSSI plans/risque/attribution/mémoire en un tour.\n\n",
        "NB : pour compléter le cockpit, utilise get_indicator_context via `tool_calls` ;\n"
        "la tournée d'outils te donne AUSSI plans/risque/attribution/mémoire en un tour.\n\n",
    )


def build_batch_prompt(
    *,
    mandate: str,
    memory: str,
    shared_context: dict,
    symbols_payload: list[dict],
    allow_context_request: bool = False,
    allow_tool_calls: bool = False,
    use_symbol_calls_contract: bool = False,
    max_tool_calls_per_symbol: int = 3,
    max_rounds: int = 1,
) -> str:
    """Prompt batch : contexte PARTAGÉ (cockpit/portefeuille/KPI/attribution/learnings)
    envoyé UNE fois, puis la liste des symboles à décider -> un seul appel modèle."""
    if use_symbol_calls_contract:
        contract = _symbol_calls_final_contract(allow_tool_calls, max_rounds=max_rounds)
    else:
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
        f"{_decision_guidance(allow_context_request=allow_context_request)}"
        f"{_indicator_watch_vocabulary()}"
        f"{_tool_catalog(allow_context_request=allow_context_request, max_tool_calls_per_symbol=max_tool_calls_per_symbol, max_rounds=max_rounds) if allow_tool_calls else ''}"
        f"# Contexte partagé (JSON)\n{json.dumps(shared_context, ensure_ascii=False)}\n\n"
        f"# Symboles à décider (JSON)\n{json.dumps(symbols_payload, ensure_ascii=False)}\n\n"
        f"# Contrat de sortie\n{contract}\n"
    )
