"""Prompt builder for machine-owned learning consolidation."""

from __future__ import annotations

import json

from trader.agent.learnings.consolidation_stores import DEFAULT_MAX_BY_SYMBOL, DEFAULT_MAX_GLOBAL


def build_consolidation_prompt(
    current: dict,
    new_raw: list[dict],
    *,
    attribution: dict | None = None,
    meta_performance: dict | None = None,
) -> str:
    payload = {
        "current_consolidated": current,
        "new_raw_learnings": new_raw,
        "attribution": attribution,
        "meta_performance": meta_performance,
        "schema": {
            "global": [{"note": "string", "robustness": "optional string"}],
            "by_symbol": {"SYMBOL": [{"note": "string", "robustness": "optional string"}]},
        },
        "limits": {"global": DEFAULT_MAX_GLOBAL, "by_symbol": DEFAULT_MAX_BY_SYMBOL},
    }
    return (
        "Tu es le consolidateur machine de casys-trader.\n"
        "Objectif : produire un résumé actionnable, équilibré et non redondant.\n"
        "Règles strictes :\n"
        "1. ANCRE dans les RÉSULTATS. Les notes brutes sont du contexte, pas une "
        "preuve. Mets `robustness:\"high\"` seulement si l'attribution confirme le "
        "pattern avec au moins 3 occurrences distinctes ET un P&L cohérent.\n"
        "2. ÉQUILIBRE entrée / sortie / coût. Les patterns d'ENTRÉE POSITIFS disent "
        "quand AGIR et viennent des trades GAGNANTS de l'attribution "
        "(CLOSE/take_profit/max_hold). Ajoute des patterns de GESTION DE SORTIE "
        "quand un `exit_reason` coûte dans `by_exit_reason` (lis `total_pnl` comme "
        "net et `total_commission` comme commission, ex. trailing_stop). Ajoute des "
        "patterns de COÛT si `total_commissions` ronge une part notable de "
        "`realized_gross_pnl`.\n"
        "3. Lis les stats META comme descriptives, pas comme des règles. "
        "`meta_performance` groupe les décisions par action et `reason_code`; "
        "un `HOLD missed` élevé signale une catégorie d'abstention à expliquer "
        "(ex. attendre pullback qui rate le move), pas une obligation de trader.\n"
        "4. QUOTA anti-abstention : AU MOINS 3 règles `global` doivent être des "
        "conditions d'ACTION positives ; AU PLUS 4 règles d'abstention dans "
        "`global`.\n"
        "5. ANTI-REDONDANCE : une règle `by_symbol` n'est gardée que si elle dit "
        "quelque chose de SPÉCIFIQUE au symbole, absent du `global`. Interdit de "
        "reformuler une règle globale par symbole.\n"
        "6. Garde les limites : fusionne les abstentions redondantes, omets ce qui "
        "répète `current_consolidated`, respecte DEFAULT_MAX_GLOBAL et "
        "DEFAULT_MAX_BY_SYMBOL, et retourne une sortie JSON pure {global, by_symbol} "
        "sans markdown.\n"
        "7. TRANSPORT STRICT : Ne produis aucun message de statut, aucune explication, "
        "aucun appel outil, aucun markdown. Réponds par un seul message assistant "
        "contenant uniquement du JSON pur conforme au schema, car stdout est parsé "
        "automatiquement.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
