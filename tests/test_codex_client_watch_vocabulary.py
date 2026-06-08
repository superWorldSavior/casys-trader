"""Le prompt batch (chemin de prod) doit exposer le vocabulaire valide des
`indicator_watch`, dérivé des sources de vérité, pour que l'agent ne se fasse
pas rejeter sur le format (cf rejet réel GC=F : `er`/`ac` au lieu de
`efficiency_ratio`/`autocorrelation`, opérateur `eq` inexistant)."""

from trader.codex_client import build_batch_prompt, build_prompt
from trader.features import DEFAULT_INDICATORS
from trader.indicator_watch import WATCH_VALID_OPERATORS
from trader.agent_context import INDICATOR_COLUMNS


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
