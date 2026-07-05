"""data_source — adapters and config for market data sources.

AX / Contrats étroits  : Protocol minimal (get_bars seul).
AX / Explicit over Implicit : aucun défaut magique de source.
AX / Machine-Readable Errors : toutes les erreurs sont MarketError(code, context).
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from pathlib import Path
from typing import Callable

from trader.domain.market_data import Bar, MarketError
from trader.market.ports import DataSource as DataSource

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YFinanceDataSource
# ---------------------------------------------------------------------------

class YFinanceDataSource:
    """Wrapper mince autour de market.get_bars (yfinance).

    Aucune logique propre : délègue intégralement à market.get_bars
    pour bénéficier de l'agrégation 4h existante.
    """

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> list[Bar]:
        from trader.market import market_data as market
        return market.get_bars(symbol, lookback, interval)


# ---------------------------------------------------------------------------
# CompositeDataSource
# ---------------------------------------------------------------------------

class CompositeDataSource:
    """Route chaque symbole vers une liste ordonnée de sources.

    Config de routes (première qui matche gagne, fnmatch) :
        routes = [
            {"symbols": ["EURUSD=X", "USDJPY=X"], "sources": ["ib", "yfinance"]},
            {"symbols": ["*"],                      "sources": ["yfinance", "ib"]},
        ]
        sources = {"ib": IBDataSource(...), "yfinance": YFinanceDataSource()}

    Fallback déclenché si :
      - la source lève une exception (n'importe laquelle)
      - la source retourne des barres jugées stale par market.assess_freshness

    Source listée dans une route mais absente du dict sources → sautée
    (log machine-readable, pas d'exception).

    Toutes en échec → MarketError("all_sources_failed", ...).
    Symbole sans route → MarketError("no_route", ...).
    """

    # Budget de fraîcheur utilisé pour évaluer les barres avant fallback.
    # Valeur conservative (15 min de grâce) : si une source retourne des barres
    # plus vieilles que l'intervalle demandé + 15 min → stale → fallback.
    _FRESHNESS_GRACE_MINUTES = 15.0

    def __init__(
        self,
        *,
        routes: list[dict],
        sources: dict[str, object],
    ) -> None:
        """
        Args:
            routes:  Liste ordonnée de routes.
                     Chaque route : {"symbols": [str], "sources": [str]}.
            sources: Dict {nom: DataSource} des sources disponibles.
        """
        self._routes = routes
        self._sources = sources
        self._last_source: dict[str, str] = {}
        self._failed_sources_since_last_check: dict[str, MarketError] = {}
        # Thread-safety (W3) : le daemon et N workers de file peuvent appeler get_bars /
        # consume_failed_sources en parallèle. Verrou LOCAL à l'objet (≠ permit de file) →
        # sérialise les mutations d'état, sans risque de deadlock avec la ressource acpx.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> list[Bar]:
        """Retourne les barres pour symbol en essayant les sources dans l'ordre.

        Thread-safety (W3) : l'I/O réseau (`source.get_bars`) se fait **hors verrou** —
        les fetchs concurrents (daemon + N workers de file) tournent en parallèle et un
        hang réseau n'en gèle qu'un. Seules les mutations d'état partagé (`_last_source`,
        `_failed_sources`) passent par un verrou COURT (quelques µs), via `_mark_served` /
        `_mark_failed` / `consume_failed_sources`. Le verrou est local à l'objet (≠ permit
        de file) → aucune acquisition imbriquée avec `acpx`, pas de deadlock (spec §5.1).

        Raises:
            MarketError("no_route"):          aucune route ne matche symbol.
            MarketError("all_sources_failed"): toutes les sources ont échoué.
        """
        from datetime import datetime, timezone
        from trader.market import market_data as market

        source_names = self._resolve_route(symbol)
        if source_names is None:
            raise MarketError("no_route", f"{symbol}: aucune route ne matche")

        max_age = market.freshness_budget_minutes(interval, grace_minutes=self._FRESHNESS_GRACE_MINUTES)
        now = datetime.now(timezone.utc)

        last_error: MarketError | None = None
        # F6 : meilleur résultat stale disponible (premier rencontré).
        # Retourné si toutes les sources échouent ou sont stale — le daemon décide.
        best_stale: tuple[str, list[Bar]] | None = None

        for name in source_names:
            source = self._sources.get(name)
            if source is None:
                log.debug(
                    '{"event":"source_skipped","symbol":"%s","source":"%s",'
                    '"reason":"absent_du_dict"}',
                    symbol, name,
                )
                continue

            try:
                bars = source.get_bars(symbol, lookback, interval)
            except Exception as exc:  # noqa: BLE001
                last_error = (
                    exc if isinstance(exc, MarketError)
                    else MarketError("source_error", f"{symbol}@{name}: {exc}")
                )
                self._mark_failed(name, last_error)
                log.warning(
                    '{"event":"source_fallback","symbol":"%s","source":"%s",'
                    '"reason":"exception","code":"%s"}',
                    symbol, name,
                    last_error.code,
                )
                continue

            # Garde fraîcheur : barres stale → fallback, mais garder comme candidat
            freshness = market.assess_freshness(bars, now=now, max_age_minutes=max_age)
            if not freshness.fresh:
                last_error = MarketError(
                    "stale_data",
                    f"{symbol}@{name}: reason={freshness.reason} age={freshness.age_minutes}min",
                )
                log.debug(
                    '{"event":"source_fallback","symbol":"%s","source":"%s",'
                    '"reason":"stale","stale_reason":"%s"}',
                    symbol, name,
                    freshness.reason,
                )
                # F6 : conserver le premier résultat stale (meilleur effort)
                if best_stale is None:
                    best_stale = (name, bars)
                continue

            # Succès
            self._mark_served(symbol, name)
            return bars

        # F6 : si au moins une source a retourné des barres (même stale) → les retourner.
        # Le daemon (couche métier) gère la fraîcheur ; le composite ne sait pas si
        # c'est hors-séance ou une vraie panne.
        if best_stale is not None:
            stale_name, stale_bars = best_stale
            self._mark_served(symbol, stale_name)
            return stale_bars

        # Aucune barre disponible du tout (toutes les sources ont levé exception)
        if last_error is None:
            last_error = MarketError("all_sources_failed", f"{symbol}: toutes sources absentes ou sautées")
        raise MarketError(
            "all_sources_failed",
            f"{symbol}: toutes sources épuisées — dernier: {last_error.code}: {last_error.context}",
        )

    def last_source(self, symbol: str) -> str | None:
        """Retourne le nom de la source qui a servi symbol lors du dernier appel."""
        with self._lock:
            return self._last_source.get(symbol)

    def _mark_served(self, symbol: str, name: str) -> None:
        """Enregistre la source qui a servi symbol (verrou court, hors I/O)."""
        with self._lock:
            self._last_source[symbol] = name

    def _mark_failed(self, name: str, error: MarketError) -> None:
        """Enregistre l'échec d'une source depuis le dernier relevé (verrou court, hors I/O)."""
        with self._lock:
            self._failed_sources_since_last_check[name] = error

    def consume_failed_sources(self) -> dict[str, MarketError]:
        """Retourne et vide les dernières erreurs par source depuis le dernier relevé."""
        with self._lock:
            failed = dict(self._failed_sources_since_last_check)
            self._failed_sources_since_last_check.clear()
            return failed

    def disconnect(self) -> None:
        """Déconnecte chaque source unique qui expose disconnect().

        Best-effort : les exceptions sont avalées (une source en erreur n'empêche
        pas les autres d'être déconnectées). Déduplique les instances partagées.
        """
        seen: set[int] = set()
        for source in self._sources.values():
            source_id = id(source)
            if source_id in seen:
                continue
            seen.add(source_id)
            disconnect_fn = getattr(source, "disconnect", None)
            if disconnect_fn is None:
                continue
            try:
                disconnect_fn()
            except Exception:  # noqa: BLE001 — best-effort
                pass

    # ------------------------------------------------------------------
    # Routing interne
    # ------------------------------------------------------------------

    def _resolve_route(self, symbol: str) -> list[str] | None:
        """Retourne la liste de sources de la première route qui matche symbol."""
        for route in self._routes:
            for pattern in route["symbols"]:
                if symbol == pattern or fnmatch.fnmatch(symbol, pattern):
                    return list(route["sources"])
        return None


# ---------------------------------------------------------------------------
# Indirection thread-safe pour le worker de file (grain-1 queue)
# ---------------------------------------------------------------------------

def make_indirect_get_bars(
    get_data_source: Callable[[], "DataSource | None"],
) -> Callable[..., list[Bar]]:
    """Adapte un data_source à référence VARIABLE en un `get_bars` stable et injectable.

    Le daemon (re)construit ou annule `data_source` en cours de run (reconnexion,
    reconfiguration) et le pool de workers démarre avant sa première construction : on
    ne capture donc jamais l'objet, on lit la ref COURANTE via `get_data_source`. `None`
    (pas encore prêt / déconnecté) → `MarketError`, absorbée en 'unavailable' côté outil
    par `resolve_indicator_requests`.

    La thread-safety est assurée en amont par le verrou interne de `CompositeDataSource`
    (le point d'entrée réel en paper) ; ce helper ne gère QUE le cycle de vie de la ref.
    """

    def get_bars(symbol: str, lookback: str = "5d", interval: str = "1h") -> list[Bar]:
        source = get_data_source()
        if source is None:
            raise MarketError("data_source_unavailable", "aucun data_source courant")
        return source.get_bars(symbol, lookback=lookback, interval=interval)

    return get_bars


# ---------------------------------------------------------------------------
# Loader config YAML
# ---------------------------------------------------------------------------

def parse_data_sources_config(
    config_path: Path | str,
    *,
    profile_override: str | None = None,
    known_source_names: frozenset[str] | None = None,
) -> tuple[list[dict], str]:
    """Lit et valide config/data_sources.yaml ; retourne (routes_raw, profile_name).

    Sépare le parsing structurel (appelé au démarrage, fail-fast) de la
    construction des sources (connexion IB, etc.) qui a lieu dans la boucle.

    Raises:
        MarketError("config_not_found"):  fichier absent.
        MarketError("invalid_config"):    YAML invalide ou structure manquante.
        MarketError("unknown_profile"):   profil demandé absent des profils déclarés.
        MarketError("unknown_source"):    source de route hors de known_source_names.
    """
    import yaml as _yaml

    p = Path(config_path)

    if not p.exists():
        raise MarketError("config_not_found", str(p))

    try:
        raw = _yaml.safe_load(p.read_text(encoding="utf-8"))
    except _yaml.YAMLError as exc:
        raise MarketError("invalid_config", f"{p}: {exc}") from exc

    if not isinstance(raw, dict):
        raise MarketError("invalid_config", f"{p}: racine non-dict")

    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        raise MarketError("invalid_config", f"{p}: clé 'profiles' manquante ou non-dict")

    profile_name = profile_override or raw.get("profile")
    if not profile_name:
        raise MarketError("invalid_config", f"{p}: clé 'profile' manquante et aucun override")

    if profile_name not in profiles:
        raise MarketError(
            "unknown_profile",
            f"profil={profile_name!r} absent de {sorted(profiles)}",
        )

    profile_cfg = profiles[profile_name]
    routes_raw = profile_cfg.get("routes", [])
    if not isinstance(routes_raw, list):
        raise MarketError("invalid_config", f"{p}: profil {profile_name!r}: 'routes' non-list")

    if known_source_names is not None:
        for route in routes_raw:
            for src_name in route.get("sources", []):
                if src_name not in known_source_names:
                    raise MarketError(
                        "unknown_source",
                        f"source={src_name!r} dans profil {profile_name!r} absente de known_source_names={sorted(known_source_names)}",
                    )

    return routes_raw, profile_name


def load_composite_from_config(
    config_path: Path | str,
    *,
    available_sources: dict[str, object],
    profile_override: str | None = None,
    known_source_names: frozenset[str] | None = None,
) -> tuple[CompositeDataSource, str]:
    """Charge config/data_sources.yaml et retourne (CompositeDataSource, profil_utilisé).

    Délègue la validation structurelle à parse_data_sources_config puis
    construit le composite avec les sources disponibles au runtime.

    Args:
        config_path:         Chemin absolu ou relatif vers le YAML.
        available_sources:   Dict {nom: DataSource} des sources construites au démarrage.
        profile_override:    Si fourni, force le profil.
        known_source_names:  Registre des sources légales (détection de fautes de frappe).

    Returns:
        (composite, profile_used)

    Raises: voir parse_data_sources_config.
    """
    routes_raw, profile_name = parse_data_sources_config(
        config_path,
        profile_override=profile_override,
        known_source_names=known_source_names,
    )
    composite = CompositeDataSource(routes=routes_raw, sources=available_sources)
    return composite, profile_name
