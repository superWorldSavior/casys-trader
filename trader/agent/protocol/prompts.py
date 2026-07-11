"""Prompt builders and output contracts for the trading agent."""

from __future__ import annotations

import json
import os

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


def _prompt_json(value: object) -> str:
    """JSON compact pour le transport LLM, sans changer le contrat semantique."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

_OUTPUT_CONTRACT = (
    "Réponds UNIQUEMENT par un objet JSON valide, sans texte autour, de la forme:\n"
    '{"symbol":"<SYM>","confidence":<0..1>,"rationale":"<court>",'
    f'"decision_reason_code":"{_REASON_CODE_ENUM}","calls":[<tool_call>,...]}}\n'
    "`calls: []` signifie HOLD explicite pour ce symbole.\n"
    "Optionnel : `applied_learning_ids:[\"<rule_id>\",...]` cite au plus trois "
    "règles de `context.learnings.global` qui ont réellement pesé sur cette décision.\n"
    "Grammaire Pine-like JSON officielle: `strategy_entry`, `strategy_exit`, "
    "`strategy_close`, `set_next_wake`, `propose_indicator_watch`, "
    "`cancel_watch`, `record_learning`. Pense à ces calls comme à un MCP JSON "
    "inspiré de Pine Script : utilise ton intuition "
    "strategy.entry/strategy.exit/strategy.close, mais rends uniquement les appels "
    "JSON, jamais du code Pine Script.\n"
    "- strategy_entry{id?, direction:\"long|short\", qty?, risk_pct?, exit?, thesis?} "
    "= changer l'exposition ; même sens = renforcement, sens opposé = retournement.\n"
    "- strategy_exit{id?, limit?, stop?, qty_percent?, trail?, trail_offset?, protect?, "
    "exit_watch?, max_hold_minutes?} = patcher la règle de sortie d'une position ouverte.\n"
    "- strategy_close{id?, qty?, qty_percent?} = sortie marché immédiate position-aware.\n"
    "- set_next_wake{minutes|on|when}, propose_indicator_watch{...}, cancel_watch{id|ids|watch_ids}, "
    "record_learning{note} couvrent respectivement réveil, plan armé, annulation et mémoire.\n"
)

_COMPACT_OUTPUT_CONTRACT = (
    _OUTPUT_CONTRACT
    + "Option B, seulement si un indicateur précis manque pour décider, demande un "
    "complément borné:\n"
    '{"symbol": "<SYM>", "action": "REQUEST_CONTEXT", "rationale": "<pourquoi>", '
    f'"requests": [{{"symbol": "<SYM>", "indicators": ["{_WATCH_INDICATOR_ENUM}"], '
    '"timeframe": "15m|30m|1h|4h|1d", "lookback": "5d|1mo|3mo|6mo|1y", '
    '"window": 48, "as_of": "latest"}]}\n'
    "Ne demande jamais de barres brutes. Demande peu d'indicateurs, sur peu de symboles. "
    "Si le marché est mort ou sans edge, rends `calls: []` avec un prochain réveil plus lent."
)


_DECISION_GUIDANCE = (
    "# Ton échelle d'engagement (du jugement immédiat au scénario délégué)\n"
    "À chaque réveil, choisis le bon outil — pas par défaut le premier :\n"
    "1. DÉCIDER maintenant (`strategy_entry`, `strategy_close` ou `strategy_exit`) : "
    "l'edge est là, tout de suite.\n"
    "2. VEILLER (`set_next_wake` ou `propose_indicator_watch` on_trigger=WAKE) : une question au marché — "
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
    "Tu as 3 niveaux de contexte plans/veilles. Niveau détail local : `active_watches` "
    "(fourni par symbole) liste les veilles et plans armés ACTIFS du symbole "
    "décidé : id, kind, intent, conditions, expiration. Niveau résumé focalisé : "
    "`context.active_plans_summary` donne les compteurs globaux et les plans du "
    "symbole décidé ; utilise-le comme garde anti-doublon/OCO avant d'empiler. "
    "Niveau détail global : "
    "l'outil `get_active_plans` donne à la demande le détail complet des "
    "TradePlans ouverts en portée globale. N'appelle `get_active_plans` QUE si "
    "le résumé focalisé (`active_plans_summary`) et ta vue locale ne suffisent pas "
    "— p.ex. pour lire le détail d'un plan sur un AUTRE symbole. Sinon, n'y "
    "recours pas. Pour abandonner un plan, utilise "
    "`cancel_watch` avec son `id`. Corriger un plan = l'annuler (`cancel_watch`) "
    "ET reposer une veille/plan à jour (`propose_indicator_watch`) dans la même "
    "décision.\n\n"
    "# Semantic layer\n"
    "Les indicateurs fiables sont calculés par le code. Le prompt expose "
    "`context.cockpit` (compact, sans barres brutes). Si ce cockpit ne suffit pas, "
    "demande un complément borné via REQUEST_CONTEXT ; le daemon injectera "
    "`context.research` avec les indicateurs calculés, et `context.prior_rationale` "
    "(ta demande initiale) pour reprendre ton raisonnement sans repartir de zéro.\n\n"
    "# Plans de sortie\n"
    "Quand tu ouvres, renforces ou retournes une position avec `strategy_entry`, "
    "fournis si possible un `exit` structuré utile : stop, limit/tp, trail, protect et/ou "
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
    "`set_next_wake{minutes}` court que si ton plan l'exige vraiment ; sinon "
    "préfère poser une veille (`set_next_wake{when}` ou `propose_indicator_watch`) et laisser le réveil événementiel "
    "travailler — chaque réveil que tu demandes consomme un appel modèle.\n"
    "Pour chaque symbole tu peux soit fixer `set_next_wake{minutes}`, soit poser "
    "une condition multi-timeframe. Une watch est évaluée "
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
    "ajuste ta thèse et utilise `record_learning` quand un apprentissage nouveau "
    "mérite d'être conservé. `context.learnings` te rappelle "
    "tes notes précédentes avec leur issue.\n\n"
    "# Compétence, situation et expérience\n"
    "`context.learnings.global` et `guardrails` sont des principes transversaux : "
    "ils disent comment trader, jamais ce qui se passe maintenant sur un titre. "
    "La situation micro de référence est `universe_mandate.symbol_mandate.company_context`, "
    "figée avec le mandat. `company_intelligence_delta`, quand il existe, est une mise à jour "
    "micro plus fraîche et bornée, pas un second brief. `universe_mandate` porte le "
    "mandat stratégique de l'agent univers — role, posture, directional_view, "
    "family_context et portfolio_context (advisory) — jamais un ordre, une quantité "
    "ni une copie du micro brut ; tu peux le contredire si ta structure locale ne "
    "confirme pas. Si une règle globale a réellement "
    "pesé sur ta décision, cite son `rule_id` dans `applied_learning_ids` (au plus "
    "trois IDs présents dans le contexte) ; ne cite jamais une règle simplement "
    "visible. Pour une analogie de setup plus précise, approfondis avec "
    "`recall_learnings` et un petit query ciblé par symbole, famille et setup. "
    "N'appelle pas ce recall mécaniquement sur un réveil sans enjeu et ne traite "
    "jamais une expérience historique comme une actualité du symbole.\n\n"
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
    "Ses prix et niveaux (`stop`, `limit`, swings) sont dans la "
    "devise native du titre (`ccy`), PAS en USD. `qty` est un nombre "
    "d'unités du titre, jamais un montant en devise. "
    "Dimensionne en unités du titre à partir de ta thèse : distance au hard_stop, "
    "conviction, frais et volatilité. Consulte ensuite "
    "`context.risk_capacity.per_symbol[SYM]` : `max_buy_qty` et `max_sell_qty` "
    "sont une capacité d'exécution calculée par le runtime, pas une consigne "
    "stratégique. Pour `strategy_entry.direction:\"long\"`, ta `qty` ne doit pas dépasser "
    "`max_buy_qty`; pour `direction:\"short\"`, elle ne doit pas dépasser "
    "`max_sell_qty`. Si cette capacité est positive, une petite entrée reste "
    "possible quand la thèse la justifie. "
    "Le portefeuille global est en USD pour ta vue d'ensemble : "
    "`cash_ledger`/`cash` est le ledger broker et peut inclure le produit des shorts; "
    "`cash_available` est net de l'exposition short courante et représente mieux "
    "le cash libre. Ces champs décrivent l'état du portefeuille et la liquidité; "
    "ils ne sont pas une règle de sizing ni une obligation de HOLD. Tu ne "
    "convertis JAMAIS, tout est déjà fourni dans la bonne devise. Le daemon "
    "applique les fusibles currency-correct à l'exécution.\n\n"
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


def _exec_guidance() -> str:
    """Consigne d'exec — présente UNIQUEMENT si l'exec en cage est activé.

    Gaté sur le même flag que le transport (``CASYS_AGENT_EXEC``) : sans lui,
    l'outil python n'existe pas côté agent, donc l'annoncer le pousserait à
    appeler un outil absent et à casser le contrat de sortie JSON.
    """
    if os.getenv("CASYS_AGENT_EXEC", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return ""
    return (
        "# Calcul déterministe\n"
        "Tu disposes d'un python EN CAGE (réseau coupé, écriture confinée) pour tes "
        "calculs. Pour tout NOMBRE que tu poserais sinon de tête et qu'aucun indicateur "
        "fourni ne donne (z-score, corrélation, distance en ATR à un niveau, stats sur "
        "les barres), calcule-le en python plutôt que l'estimer. Les exécutions sont des "
        "tours intermédiaires : ta réponse FINALE reste un objet JSON pur, sans texte "
        "autour.\n\n"
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
        f"{_exec_guidance()}"
        f"{_indicator_watch_vocabulary()}"
        f"# Contexte marché et portefeuille (JSON)\n{_prompt_json(context)}\n\n"
        f"# Contrat de sortie\n{output_contract}\n"
    )


_BATCH_FINAL_CONTRACT = (
    'Réponds UNIQUEMENT par {"decisions": [ <obj>, ... ]} avec EXACTEMENT une entrée '
    "par symbole listé.\n"
    'Chaque <obj>: {"symbol":"<SYM>","confidence":<0..1>,"rationale":"<court>",'
    f'"decision_reason_code":"{_REASON_CODE_ENUM}","calls":[<tool_call>,...]}}\n'
    "`calls: []` signifie HOLD explicite. Utilise la grammaire Pine-like JSON "
    "`strategy_entry` / `strategy_exit` / `strategy_close` pour les décisions de trading. "
    "Optionnel : `applied_learning_ids` contient au plus trois `rule_id` réellement "
    "utilisés, présents dans `context.learnings.global`."
)

_SYMBOL_CALLS_FINAL_CONTRACT = (
    'Le contrat final: {"decisions": [ <obj>, ... ]} avec EXACTEMENT une entrée '
    "par symbole listé.\n"
    'Chaque <obj>: {"symbol":"<SYM>","confidence":<0..1>,"rationale":"<court>",'
    f'"decision_reason_code":"{_REASON_CODE_ENUM}","calls":[<tool_call>,...]}}\n'
    "`calls: []` signifie HOLD explicite pour ce symbole. Ne mélange pas `calls` "
    "avec les anciens champs métier. `applied_learning_ids` est optionnel : au plus "
    "trois `rule_id` de `context.learnings.global` qui ont réellement pesé sur ta "
    "décision.\n"
    "Action tools finaux autorisés par symbole:\n"
    "Grammaire Pine-like JSON officielle: position intent = changer l'exposition; "
    "exit rule = règle attachée à une position ouverte; "
    "review wake = reconsultation par le LLM; "
    "armed plan = exécution daemon sans reconsultation.\n"
    "Pense à ces calls comme à un MCP JSON inspiré de Pine Script : utilise ton intuition "
    "strategy.entry/strategy.exit/strategy.close, mais rends uniquement les appels JSON ci-dessous, "
    "jamais du code Pine Script.\n"
    "Mapping: strategy_entry = position intent; strategy_exit = exit rule; "
    "strategy_close = sortie marché immédiate; set_next_wake = review wake; propose_indicator_watch = armed plan; "
    "cancel_watch annule une veille ou un plan armé; record_learning note un apprentissage.\n"
    "- strategy_entry{id?, direction:\"long|short\", qty?, risk_pct?, exit?, thesis?} : "
    "entrée Pine-like; le daemon reste seul exécuteur et applique execution/exit/RiskGate. "
    "direction long→BUY et short→SELL; si une position existe déjà, même sens = renforcement, "
    "sens opposé = retournement. "
    "Sizing : au lieu de calculer qty toi-même, tu peux "
    "fournir risk_pct (ex: 0.005 = 0.5 % de l'equity) et omettre qty : le daemon "
    "dérive qty = risk_pct × equity / (distance_stop × fx_rate). "
    "Si tu fournis risk_pct sans qty, un hard_stop est nécessaire pour dériver la quantité ; "
    "risk_pct sans qty est réservé à une nouvelle entrée flat ; pour renforcer/retourner une position existante, fournis qty. "
    "avec qty explicite, le hard_stop reste recommandé mais n'est pas requis en mode exploration. "
    "max_risk_per_trade_pct est une borne indicative : si le risque calculé la dépasse, "
    "le daemon signale un warning sans bloquer l'ordre. "
    "qty explicite prime toujours sur risk_pct (Explicit Over Implicit). "
    "Pour une nouvelle entrée avec bracket/protection, mets les règles dans strategy_entry.exit ; "
    "ne combine pas strategy_entry et strategy_exit sur le même symbole. "
    '`thesis` est OPTIONNEL : {setup:"<setup court>", horizon:"intraday|swing|position", '
    "invalidation:\"<condition d'invalidation>\"} — tag structuré persisté pour l'attribution "
    "et la boucle d'apprentissage. N'en ajoute un que si la thèse est claire.\n"
    "- strategy_exit{id?, from_entry?, limit?, stop?, qty_percent?, trail?, trail_offset?, protect?, exit_watch?, max_hold_minutes?} : "
    "strategy.exit Pine-like; patche le plan de sortie du symbole déjà ouvert. "
    "limit+stop dans strategy_exit = bracket de sortie (TP + stop), pas un stop-limit. "
    "qty_percent omis = 100; pour l'instant qty_percent ne s'applique qu'à limit seul pour un scale-out; "
    "limit+stop avec qty_percent<100 est rejeté pour l'instant "
    "partial_bracket_exit_not_supported tant que la réservation/OCA partielle n'est pas native. "
    "stop avec qty_percent<100 est rejeté partial_stop_exit_not_supported. "
    "Sur position ouverte, un stop structurel peut aussi protéger un gain sous un swing récent, "
    "tant que le niveau résolu reste du bon côté du prix courant. "
    "No-op tracé si pas de plan ouvert. "
    "`calls:[{strategy_exit}]` = HOLD + ajustement de gestion actif.\n"
    "- strategy_close{id?, qty?, qty_percent?} : sortie marché immédiate. "
    "Sans taille, ferme toute la position ; qty_percent<100 réduit une fraction ; qty réduit une quantité absolue.\n"
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
    "strategy.exit Pine: limit = take-profit absolu; stop = stop absolu ou "
    "{struct:\"swing_low|swing_high|vwap\",window,buffer_pct?}; "
    "trail = {type:\"price|percent|volatility_multiple\",value} ou trail_offset; "
    "protect = {arm_r,giveback,close_fraction?,lock_r?,min_hold_minutes?}; "
    "from_entry cible l'entrée/plan courant; exit_watch et max_hold_minutes restent des règles optionnelles de review/temps. "
    "protect.lock_r verrouille le stop à +N R (0 = breakeven); "
    "Exemple compact: "
    '{"symbol":"DASH","confidence":0.74,"rationale":"breakout propre",'
    f'"decision_reason_code":"ENTRY_SIGNAL","calls":[{{"tool":"strategy_entry","args":'
    '{"id":"long","direction":"long","qty":20,"exit":{"id":"bracket","limit":78.5,'
    '"stop":{"struct":"swing_low","window":24},"protect":{"arm_r":1.0,"giveback":0.35,"lock_r":0.25}}}}},'
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
        "min_pct/max_pct sont des bornes indicatives : si ton niveau résolu sort de "
        "ces bornes, l'écart est signalé en warning, pas rejeté. Le daemon ne déplace jamais "
        "le hard_stop pour le faire rentrer dans une borne.\n"
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
        'Pour FLIP, garde un hard_stop prix résolu: nombre > 0 OU {type:"price", price:<requis, >0>}.\n'
        f'`trailing_stop`: null OU {{trail_type:"{trail_type_enum}", '
        "trail_value:<requis, >0>}. "
        "`trail_type` doit être exactement l'un de cet enum. "
        "Unités trail_value: percent = fraction (0.004 = 0.4%); "
        "price = distance absolue en prix; volatility_multiple = multiple de la "
        "volatilité récente. Sans enabled_after, le trail "
        "ne s'arme qu'une fois en profit au moins égal au trail.\n"
    )


def _batch_final_contract() -> str:
    return _SYMBOL_CALLS_FINAL_CONTRACT + _symbol_calls_exit_details()


def _batch_compact_contract() -> str:
    return _batch_final_contract() + _BATCH_COMPACT_SUFFIX


def _round_label(max_rounds: int | None) -> str:
    if max_rounds is None:
        return "autant de tournées d'outils que nécessaire"
    rounds = max(int(max_rounds), 1)
    return "une seule tournée" if rounds == 1 else f"jusqu'à {rounds} tournées"


def _symbol_calls_exit_details() -> str:
    trail_type_enum = "|".join(trade_plan.TRAILING_STOP_TRAIL_TYPES)
    return (
        "\nDétails des règles de sortie compactes pour `strategy_entry.exit` et `strategy_exit`:\n"
        '`stop`: nombre > 0 OU objet {type:"price|percent|volatility_multiple|structural", ...}. '
        '`type:"price"` passe tel quel ; tout stop relatif est résolu mécaniquement au tir. '
        "percent/volatility_multiple/structural suivent ce même mécanisme. "
        "Schéma relatif: "
        '{type:"percent", percent:<0..1>, min_pct?, max_pct?} ou '
        '{type:"volatility_multiple", multiple:<requis, >0>, min_pct?, max_pct?} ou '
        '{type:"structural", anchor:"swing_low|swing_high|vwap", window:<requis, >0>, '
        "buffer_pct?|buffer_atr?, min_pct?, max_pct?}. "
        'Raccourci accepté: {struct:"swing_low|swing_high|vwap", window, buffer_pct?}.\n'
        "`limit`: take-profit absolu. `tp`: liste d'objets "
        "{r:<risk multiple>, fraction?} ou {price:<prix>, fraction?, name?}. "
        '`take_profits`: liste d\'OBJETS {price:<requis, >0>, fraction:<optionnel, >0>} '
        'OU {type:"risk_multiple", r:<requis, >0>, fraction?}. '
        "Les TP en R sont résolus depuis la distance du stop.\n"
        f'`trail`: null OU {{type:"{trail_type_enum}", value:<requis, >0>}}; '
        "`trail_offset` accepte la même unité via trail_type/offset_type. "
        "Unités trail_value: percent = fraction (0.004 = 0.4%); "
        "price = distance absolue en prix; volatility_multiple = multiple de la volatilité récente. "
        "Sans enabled_after, le trail ne s'arme qu'une fois en profit au moins égal au trail.\n"
    )


def _symbol_calls_final_contract(allow_tool_calls: bool = False, max_rounds: int | None = 1) -> str:
    """Contrat de sortie « action tools par symbole ».

    Au 1er passage (`allow_tool_calls`), l'agent garde le choix : émettre d'abord
    une tournée d'outils lecture-seule, OU rendre directement le contrat final.
    On ne bride pas ce choix (AX : faire confiance à l'agent). Au tour final,
    plus de tournée — la réponse `decisions` est imposée.
    """
    if allow_tool_calls:
        if max_rounds is None:
            head = (
                "À chaque tour, tu choisis librement : soit tu demandes "
                "autant de tournées d'outils que nécessaire "
                '{"tool_calls":[...]}'
                " (cf. « Outils domaine » ci-dessus) pour aller chercher le contexte "
                "qui te manque, soit tu rends directement le contrat final ci-dessous. "
                "Décide (`decisions`) dès que tu as assez de contexte. "
                "Au tour final imposé, plus aucun outil n'est accepté.\n"
            )
        elif max(int(max_rounds), 1) == 1:
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
    return head + _SYMBOL_CALLS_FINAL_CONTRACT + _symbol_calls_exit_details()


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
        "exécutera au déclenchement SANS re-appel modèle : fournis `order` en "
        "forme Pine-like `strategy_entry` = "
        '{"direction":"long|short","qty":<number>,"confidence":<0..1>,'
        '"exit":{"stop":{"type":"price|percent|volatility_multiple|structural",...},'
        '"limit":<number?>},"rationale":"..."}. '
        "Contrat strict à l'armement : `exit.stop` peut être en prix OU relatif, "
        "stop requis, qty>0, "
        "confidence explicite — sinon la watch est dégradée en WAKE_WITH_ORDER_INTENT "
        "(l'ordre repassera par toi). Schéma relatif armé : "
        '`stop` {type:"percent", percent:<0..1>, min_pct?, max_pct?} ou '
        '{type:"volatility_multiple", multiple:<requis, >0>, min_pct?, max_pct?} ou '
        '{type:"structural", anchor:"swing_low|swing_high|vwap", '
        "window:<requis, >0>, buffer_pct?|buffer_atr?, min_pct?, max_pct?}; "
        '`tp[]` peut utiliser {type:"risk_multiple", r:<requis, >0>, fraction?}. '
        "Le stop relatif est résolu en prix au déclenchement sur barres FRAÎCHES, puis les TP en R aussi — "
        "vrai pour percent, volatility_multiple ET structural, à égalité. "
        "min_pct/max_pct sont des bornes indicatives : si ton niveau résolu sort de "
        "ces bornes, l'écart est signalé en warning, pas rejeté ; le daemon ne déplace jamais "
        "le stop. "
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
    "- get_active_plans{symbol?,limit?} : détail complet TradePlans ouverts (portée globale)\n"
    "- get_attribution{scope:summary|confidence|exit_reason|symbol, symbol?} : perf attribuée compacte\n"
    "- describe_data{} : cube sémantique (timeframes/lookbacks valides, windows, indicateurs)\n"
    "- find_indicators{concept} : cherche des indicateurs par concept (momentum, volatilité, …)\n"
    "- get_indicator_context{symbol,indicators:[…],timeframe?,lookback?,window?,as_of?} : "
    "cube indicateurs ; tous les indicateurs gouvernés demandés sont rendus par défaut, "
    "inutile de les répartir entre plusieurs appels\n"
    "- recall_learnings{symbol?|family?|query?, limit?} : mémoire vérifiée — notes passées pondérées par leurs résultats réels\n"
    "Si tool_results contient un strategy_exit avec ok:false, c'est un retour de validation pré-exécution : "
    "corrige dans ta réponse suivante (stop en prix absolu au lieu de structural, ou retire la contrainte non résolvable). "
    "Tu n'as PAS besoin de redemander le plan, il est déjà dans ton contexte.\n"
    "Après la tournée tu recevras `tool_results` par symbole et tu DEVRAS rendre le contrat final\n"
    "(toute nouvelle tournée sera bloquée en HOLD).\n"
    "NB : get_indicator_context est la voie moderne de REQUEST_CONTEXT (les deux marchent) —\n"
    "préfère la tournée d'outils, qui te donne AUSSI plans/attribution/mémoire en un tour.\n\n"
)


def _tool_catalog(
    *,
    allow_context_request: bool,
    max_tool_calls_per_symbol: int = 3,
    max_rounds: int | None = 1,
) -> str:
    catalog = _TOOL_CATALOG.replace(
        "Bornes : 3 appels max par symbole, 24 par lot.",
        f"Bornes : {max_tool_calls_per_symbol} appels max par symbole, 24 par lot.",
    )
    if max_rounds is None:
        catalog = catalog.replace(
            "# Outils domaine (OPTIONNELS — une seule tournée)",
            "# Outils domaine (OPTIONNELS — autant de tournées d'outils que nécessaire)",
        ).replace(
            "UNE tournée d'outils lecture-seule en répondant À LA PLACE du contrat final :",
            "autant de tournées d'outils lecture-seule que nécessaire avant de rendre le contrat final :",
        ).replace(
            "Après la tournée tu recevras `tool_results` par symbole et tu DEVRAS rendre le contrat final\n"
            "(toute nouvelle tournée sera bloquée en HOLD).\n",
            "Après chaque tournée tu recevras `tool_results` par symbole. "
            "Décide (`decisions`) dès que tu as assez de contexte. "
            "Au tour final, plus aucune tournée n'est acceptée.\n",
        )
    else:
        rounds = max(int(max_rounds), 1)
    if max_rounds is not None and rounds > 1:
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
        "préfère la tournée d'outils, qui te donne AUSSI plans/attribution/mémoire en un tour.\n\n",
        "NB : pour compléter le cockpit, utilise get_indicator_context via `tool_calls` ;\n"
        "la tournée d'outils te donne AUSSI plans/attribution/mémoire en un tour.\n\n",
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
    max_rounds: int | None = 1,
) -> str:
    """Prompt batch : contexte PARTAGÉ (cockpit/portefeuille/KPI/attribution/learnings)
    envoyé UNE fois, puis la liste des symboles à décider -> un seul appel modèle."""
    # Le contrat batch canonique est toujours symbol_calls. Le paramètre
    # `use_symbol_calls_contract` reste dans la signature pour compat d'appel,
    # mais ne réactive pas l'ancien schéma inline action/quantity/exit_plan.
    contract = _symbol_calls_final_contract(allow_tool_calls, max_rounds=max_rounds)
    return (
        "Tu es le PLANIFICATEUR d'un système de trading paper : tu conçois des "
        "scénarios — entrées armées, veilles, plans de sortie — que le daemon "
        "exécute mécaniquement ; tu n'opères pas le marché en continu. Le contexte "
        "PARTAGÉ (cockpit/radar cross-asset, portefeuille, KPI, attribution, "
        "learnings) est donné UNE fois ; rends une décision pour CHAQUE symbole "
        "de la liste.\n\n"
        f"# Mandat\n{mandate}\n\n"
        f"# Mémoire / stratégie\n{memory}\n\n"
        f"{_decision_guidance(allow_context_request=allow_context_request)}"
        f"{_exec_guidance()}"
        f"{_indicator_watch_vocabulary()}"
        f"{_tool_catalog(allow_context_request=allow_context_request, max_tool_calls_per_symbol=max_tool_calls_per_symbol, max_rounds=max_rounds) if allow_tool_calls else ''}"
        f"# Contexte partagé (JSON)\n{_prompt_json(shared_context)}\n\n"
        f"# Symboles à décider (JSON)\n{_prompt_json(symbols_payload)}\n\n"
        f"# Contrat de sortie\n{contract}\n"
    )


def build_session_followup_prompt(
    *,
    symbols_payload: list[dict],
    allow_tool_calls: bool,
) -> str:
    """Delta court pour une session ACP qui possede deja le prompt complet.

    La session conserve mandat, contexte, catalogue et contrat du premier tour.
    Répéter ces blocs à chaque résultat outil gonflerait l'historique et diluerait
    la correction ; seuls les nouveaux ``tool_results`` sont donc réinjectés.
    """

    delta = [
        {
            "symbol": item.get("symbol"),
            "tool_results": list(item.get("tool_results") or []),
        }
        for item in symbols_payload
    ]
    if allow_tool_calls:
        instruction = (
            'Tu peux soit demander un nouveau {"tool_calls":[...]} si un fait '
            'matériel manque encore, soit rendre {"decisions":[...]} selon le '
            "contrat initial dès que le contexte suffit."
        )
    else:
        instruction = (
            'Tour final : aucun nouveau "tool_calls" n\'est accepté. Rends '
            'uniquement {"decisions":[...]} selon le contrat initial.'
        )
    return (
        "Suite de la MÊME décision dans la session ACP courante. Le mandat, le "
        "contexte partagé, les faits de base, le catalogue et le contrat du "
        "premier message restent en vigueur. Ne repars pas de zéro.\n\n"
        f"# Nouveaux résultats (JSON)\n{_prompt_json(delta)}\n\n"
        f"{instruction}\n"
    )
