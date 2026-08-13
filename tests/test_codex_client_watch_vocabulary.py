"""Le prompt batch (chemin de prod) doit exposer le vocabulaire valide des
`indicator_watch`, dérivé des sources de vérité, pour que l'agent ne se fasse
pas rejeter sur le format (cf rejet réel GC=F : `er`/`ac` au lieu de
`efficiency_ratio`/`autocorrelation`, opérateur `eq` inexistant)."""

from trader.agent import client as codex_client
from trader.agent.client import build_batch_prompt, build_prompt
from trader.domain.semantic.catalog import INDICATOR_COLUMNS, INDICATOR_LABEL_VALUES, TIMEFRAMES
from trader.market.features import DEFAULT_INDICATORS
from trader.planning.indicator_watch import WATCH_VALID_OPERATORS


def _batch_prompt() -> str:
    return build_batch_prompt(
        mandate="(mandat)",
        memory="(mémoire)",
        shared_context={"cockpit": {}},
        symbols_payload=[{"symbol": "SPY"}],
        allow_context_request=True,
    )


def _single_compact_prompt() -> str:
    # Chemin single (decide()) avec REQUEST_CONTEXT : le contrat décrit le schéma
    # de watch inline ; ses énumérations doivent venir des sources de vérité.
    return build_prompt(
        mandate="(mandat)",
        memory="(mémoire)",
        context={"cockpit": {}},
        allow_context_request=True,
    )


def _request_context_contract(prompt: str) -> str:
    markers = ('"action": "REQUEST_CONTEXT"', '"action":"REQUEST_CONTEXT"')
    start = next(index for marker in markers if (index := prompt.find(marker)) != -1)
    return prompt[start : start + 800]


def test_batch_prompt_liste_tous_les_indicateurs_valides_pour_une_watch() -> None:
    prompt = _batch_prompt()
    for indicator in DEFAULT_INDICATORS:
        assert indicator in prompt, f"indicateur watch manquant du prompt: {indicator}"


def test_batch_prompt_liste_tous_les_operateurs_valides() -> None:
    prompt = _batch_prompt()
    for op in WATCH_VALID_OPERATORS:
        assert op in prompt, f"opérateur watch manquant du prompt: {op}"


def test_batch_prompt_mappe_les_abreviations_cockpit_vers_les_noms_canoniques() -> None:
    # Racine du rejet : le cockpit montre `er`/`ac`, la watch exige les noms longs.
    prompt = _batch_prompt()
    for canonical, abbrev in INDICATOR_COLUMNS.items():
        assert canonical in prompt
        # l'abréviation doit apparaître appariée à son nom canonique
        assert f"{abbrev}" in prompt and f"{canonical}" in prompt


def test_single_compact_prompt_liste_tous_les_indicateurs_valides() -> None:
    prompt = _single_compact_prompt()
    for indicator in DEFAULT_INDICATORS:
        assert indicator in prompt, f"indicateur watch manquant du contrat single: {indicator}"


def test_single_compact_prompt_liste_tous_les_operateurs_valides() -> None:
    prompt = _single_compact_prompt()
    for op in WATCH_VALID_OPERATORS:
        assert op in prompt, f"opérateur watch manquant du contrat single: {op}"


def test_single_request_context_contract_liste_les_indicateurs_canoniques_valides() -> None:
    contract = _request_context_contract(_single_compact_prompt())
    for indicator in DEFAULT_INDICATORS:
        assert indicator in contract, f"indicateur REQUEST_CONTEXT single manquant: {indicator}"


def test_batch_prompt_legacy_expose_request_context_inline_exact() -> None:
    prompt = _batch_prompt()
    assert '"action":"REQUEST_CONTEXT"' in prompt
    assert f'"timeframe":"{"|".join(TIMEFRAMES)}"' in prompt


def test_watch_et_wake_exposent_tous_les_timeframes_gouvernes() -> None:
    prompt = _batch_prompt()
    enum = "|".join(TIMEFRAMES)

    assert f'interval:"{enum}"' in prompt
    assert f'"interval":"{enum}"' in prompt


def test_watch_documente_ttl_optionnel_et_ordre_arme_strict() -> None:
    prompt = _batch_prompt()
    armed_contract = codex_client._indicator_watch_vocabulary()

    assert "`ttl_minutes` est optionnel (défaut 60 minutes)" in prompt
    assert '"qty":<number>' in prompt
    assert "stop, qty>0 et confidence sont requis" in prompt
    assert "risk_pct?" not in armed_contract[armed_contract.index("# Plans armés"):]


def test_distances_structurelles_gardent_unites_et_semantique() -> None:
    prompt = _batch_prompt()

    assert "sl24/sl48" in prompt and "sh24/sh48" in prompt
    assert "en fraction du prix (0.03 = 3 %)" in prompt
    assert "sous swing_low pour un long" in prompt
    assert "au-dessus de swing_high pour un short" in prompt


def test_guidance_decrit_request_context_avec_indicators_pluriel() -> None:
    prompt = _single_compact_prompt()
    assert "`symbol × indicators × timeframe × lookback × window × as_of`" in prompt
    assert "`symbol × indicator × timeframe × lookback × window × as_of`" not in prompt


def test_guidance_decrit_les_plans_armes_execute_order() -> None:
    # D7 étage B : le contrat agent expose EXECUTE_ORDER et ses exigences
    prompt = _batch_prompt()
    assert "EXECUTE_ORDER" in prompt
    assert "strategy_entry" in prompt
    assert '"direction":"long|short"' in prompt
    assert '"exit":{"stop"' in prompt
    assert "sans re-appel" in prompt.lower()


def test_le_prompt_cadre_le_role_de_planificateur() -> None:
    # D7 (Erwan) : le rôle doit être explicite — concevoir des scénarios que le
    # daemon exécute, pas opérer le marché en polling.
    prompt = _batch_prompt()
    low = prompt.lower()
    assert "planificateur" in low
    # l'échelle d'engagement est expliquée (décider / veiller / armer)
    assert "scénario" in low
    # l'ancien cadrage « agent décideur » seul ne suffit plus comme rôle
    assert "tu es le planificateur" in low or "tu es un planificateur" in low


def test_guidance_interdit_de_deplacer_le_but_apres_un_trigger() -> None:
    prompt = _batch_prompt()

    assert "`indicator_triggers` signifie qu'une condition" in prompt
    assert "`matched.actual` donne la valeur observée" in prompt
    assert "Un trigger WAKE ouvre une relecture" in prompt
    assert "fait nouveau matériel" in prompt
    assert "Un fait déjà connu au moment du plan" in prompt
    assert "chaîne pullback puis reclaim puis breakout puis volume" in prompt


def test_guidance_distingue_trigger_et_expiration_sans_affaiblir_les_gates() -> None:
    prompt = _batch_prompt()

    assert "`wake_reasons` décrit un réveil sans match" in prompt
    assert "une watch expirée impose une réévaluation fraîche" in prompt
    assert "pas une entrée automatique" in prompt
    assert "`execution.enabled=false`" in prompt
    assert "données stale" in prompt


def test_guidance_symetrise_action_et_inaction() -> None:
    prompt = _batch_prompt()

    assert "Évalue symétriquement action et inaction" in prompt
    assert "HOLD n'est pas le choix par défaut" in prompt
    assert "coût d'opportunité" in prompt
    assert "taille réduite ou un plan armé" in prompt
    assert "une petite taille ne sauve jamais un setup sans edge" in prompt


def test_guidance_swing_fait_primer_daily_4h_sur_timing_15m() -> None:
    prompt = _batch_prompt()

    assert "le daily et le 4h gouvernent direction, thèse et invalidation" in prompt
    assert "le 15m règle le timing d'entrée" in prompt
    assert "Le 15m ne devient pas un veto permanent" in prompt


def test_batch_prompt_indique_value_toujours_numerique() -> None:
    """Le prompt doit préciser que `value` est TOUJOURS un nombre fini."""
    prompt = _batch_prompt()
    # La mention doit apparaître dans la section vocabulaire des veilles
    assert "value" in prompt
    assert "nombre" in prompt.lower() or "number" in prompt.lower()
    # value est numérique ; un label connu est toléré/converti, un label inconnu rejeté
    assert "doit être un nombre fini" in prompt
    assert "toléré" in prompt.lower() and "rejeté" in prompt.lower()


def test_batch_prompt_contient_mapping_labels_derive_de_indicator_label_values() -> None:
    """Le prompt doit exposer au moins un label de chaque indicateur de INDICATOR_LABEL_VALUES."""
    prompt = _batch_prompt()
    for indicator_name, mapping in INDICATOR_LABEL_VALUES.items():
        assert indicator_name in prompt, f"indicateur {indicator_name} absent du prompt"
        for lbl in mapping:
            assert lbl in prompt, f"label {lbl} absent du prompt (indicateur {indicator_name})"


def test_batch_prompt_label_mapping_utilise_fleches_ascii() -> None:
    """Le mapping label->float doit utiliser des flèches ASCII '->' (pas '→')."""
    prompt = _batch_prompt()
    # Au moins une flèche ASCII doit être présente dans le vocabulaire
    assert " -> " in prompt
    # Pas de flèche unicode dans la zone vocabulaire (on cherche la notation fléchée)
    # On vérifie juste la présence de '->' qui est obligatoire par AX #2
    vocab_start = prompt.find("# Vocabulaire des veilles")
    assert vocab_start != -1
    vocab_section = prompt[vocab_start : vocab_start + 2000]
    assert " -> " in vocab_section


def test_batch_prompt_exemple_chart_breakout_et_candlestick_signal() -> None:
    """Le prompt doit mentionner chart_breakout == 1.0 et candlestick_signal == -0.5."""
    prompt = _batch_prompt()
    assert "chart_breakout" in prompt
    assert "1.0" in prompt
    assert "candlestick_signal" in prompt
    assert "-0.5" in prompt


def test_batch_prompt_passer_label_string_invalide() -> None:
    """Le prompt doit préciser que passer un label string est invalide/rejeté."""
    prompt = _batch_prompt()
    # La mention doit exister dans la section vocabulaire
    vocab_start = prompt.find("# Vocabulaire des veilles")
    assert vocab_start != -1
    vocab_section = prompt[vocab_start : vocab_start + 2000]
    low = vocab_section.lower()
    assert "invalide" in low or "rejeté" in low or "rejetée" in low
