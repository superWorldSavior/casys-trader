"""Classifieur déterministe de régime de marché.

Prend un dict d'indicateurs DÉJÀ calculés (pas d'I/O, pas de fetch).
Produit un verdict lisible à 4 champs orthogonaux.

API publique :
    classify_regime(indicators: dict) -> MarketRegime

Seuils calibrés sur les échelles réelles observées dans features.py :
    - efficiency_ratio : [0, 1]  (catalog.py : output "0..1")
    - volatility       : pstdev des rendements fractionnaires close-to-close
                         → ordre ~1e-3 à ~3e-2 selon l'actif et le timeframe
    - z_score          : écarts-types (pstdev des closes bruts)
    - trend_slope      : slope OLS normalisée par |moyenne close| → petite fraction
    - chart_breakout   : {-1.0, 0.0, 1.0} ou None
    - candlestick_signal : {-1.0, -0.5, 0.0, 0.5, 1.0} ou None
"""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.market.features import compute_indicator_values

# ---------------------------------------------------------------------------
# Seuils (tunables, nommés, documentés)
# ---------------------------------------------------------------------------

# efficiency_ratio ∈ [0, 1].  Kaufman fixe généralement ~0.3-0.35 pour distinguer
# trend (ER haut) de chop (ER bas).  On choisit 0.35 comme frontière inclusive :
# ER >= ER_TREND_THRESHOLD → trending ; en dessous → range.
ER_TREND_THRESHOLD: float = 0.35

# z_score = (close - µ) / σ où σ = pstdev(closes bruts) (non des rendements).
# |z| >= 2.0 correspond à ~95% de la distribution normale → overextension.
Z_STRETCHED_THRESHOLD: float = 2.0

# volatility = pstdev(rendements close-to-close) → unité : fraction/barre.
# Sur un ETF equity 1h : ~0.003 (calme) à ~0.02 (agité). Crypto / commodities : plus élevé.
# Ces seuils sont des heuristiques de départ, à re-calibrer par actif.
#   low  : rendements quasi-plats (< 0.5 %/barre)
#   high : > 2 %/barre  (stress, gap, forte volatilité)
VOL_LOW_THRESHOLD: float = 0.005
VOL_HIGH_THRESHOLD: float = 0.020

# Mapping candlestick_signal → label lisible.
# Valeurs réelles du calcul dans features._candlestick_signal :
#   +1.0 : bullish engulfing
#   -1.0 : bearish engulfing
#   +0.5 : hammer (lower shadow >= 2×body, upper shadow <= body)
#   -0.5 : shooting star (upper shadow >= 2×body, lower shadow <= body)
#    0.0 : pas de signal
#   None : données insuffisantes
CANDLE_SIGNAL_LABELS: dict[float, str] = {
    1.0: "bullish_engulfing",
    -1.0: "bearish_engulfing",
    0.5: "hammer",
    -0.5: "shooting_star",
}

_COCKPIT_SIGNAL_INDICATORS = [
    "efficiency_ratio",
    "trend_slope",
    "chart_breakout",
    "volatility",
    "z_score",
    "candlestick_signal",
]

_HORIZON_PRIORITY = ["1d", "4h", "1h", "15m"]

_COCKPIT_CANDLE_LABELS = {
    "bullish_engulfing": "bull_engulf",
    "bearish_engulfing": "bear_engulf",
    "hammer": "hammer",
    "shooting_star": "shooting_star",
}

# ---------------------------------------------------------------------------
# Type de retour
# ---------------------------------------------------------------------------

RegimeLabel = str | None   # "trending_up" | "trending_down" | "range" | "breakout" | "unknown"
VolState = str | None      # "low" | "normal" | "high" | None
CandleLabel = str | None   # label du pattern ou None


@dataclass(frozen=True)
class MarketRegime:
    """Verdict de régime de marché — immuable, machine-readable.

    Champs :
        regime    : "trending_up" | "trending_down" | "range" | "breakout" | "unknown"
        vol_state : "low" | "normal" | "high" | None  (None si données insuffisantes)
        stretched : True/False | None  (None si z_score manquant)
        candle    : label du pattern de bougie ou None
    """

    regime: RegimeLabel
    vol_state: VolState
    stretched: bool | None
    candle: CandleLabel


# ---------------------------------------------------------------------------
# Helpers privés
# ---------------------------------------------------------------------------


def _get(indicators: dict, key: str) -> float | None:
    """Extrait une valeur float ou None depuis le dict d'indicateurs."""
    val = indicators.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _classify_regime(
    er: float | None,
    slope: float | None,
    breakout: float | None,
) -> str:
    """Logique de classification du régime.

    Priorité : breakout prime > trending (ER) > range > unknown.
    """
    # 1. Breakout prime (même si ER bas) : chart_breakout ∈ {+1.0, -1.0}
    if breakout is not None and breakout != 0.0:
        return "breakout"

    # 2. ER manquant → unknown (on ne peut pas distinguer trend de range)
    if er is None:
        return "unknown"

    # 3. Trending si ER >= seuil
    if er >= ER_TREND_THRESHOLD:
        # Direction via trend_slope : si manquant, on ne fabrique pas de direction
        if slope is None:
            return "unknown"
        # Comparaisons strictes : une pente plate (0.0 ou -0.0 après arrondi) ne
        # fabrique pas de direction -> range, plutôt qu'un "trending_up" par défaut.
        if slope > 0.0:
            return "trending_up"
        if slope < 0.0:
            return "trending_down"
        return "range"

    # 4. Sinon : range
    return "range"


def _classify_vol(vol: float | None) -> VolState:
    if vol is None:
        return None
    if vol <= VOL_LOW_THRESHOLD:
        return "low"
    if vol >= VOL_HIGH_THRESHOLD:
        return "high"
    return "normal"


def _classify_stretched(z: float | None) -> bool | None:
    if z is None:
        return None
    return abs(z) >= Z_STRETCHED_THRESHOLD


def _classify_candle(signal: float | None) -> CandleLabel:
    if signal is None:
        return None
    return CANDLE_SIGNAL_LABELS.get(signal)  # 0.0 → None (pas de pattern)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def classify_regime(indicators: dict) -> MarketRegime:
    """Classifie le régime de marché à partir d'un dict d'indicateurs calculés.

    Contrat :
    - Fonction pure : pas d'I/O, pas de datetime, pas de random.
    - Fail-safe : un indicateur absent ou None ne lève jamais d'exception.
      → champ concerné = None ou "unknown" plutôt qu'une valeur inventée.
    - Déterminisme total : mêmes entrées → mêmes sorties.

    Args:
        indicators: dict produit par compute_indicator_values ou build_indicator_snapshot.
                    Clés attendues (toutes optionnelles) :
                    efficiency_ratio, trend_slope, chart_breakout,
                    volatility, z_score, candlestick_signal.

    Returns:
        MarketRegime(regime, vol_state, stretched, candle)
    """
    er = _get(indicators, "efficiency_ratio")
    slope = _get(indicators, "trend_slope")
    breakout = _get(indicators, "chart_breakout")
    vol = _get(indicators, "volatility")
    z = _get(indicators, "z_score")
    candle_signal = _get(indicators, "candlestick_signal")

    return MarketRegime(
        regime=_classify_regime(er, slope, breakout),
        vol_state=_classify_vol(vol),
        stretched=_classify_stretched(z),
        candle=_classify_candle(candle_signal),
    )


def _horizon_indicators(bars: list[object], *, window: int) -> dict[str, float | None] | None:
    if not bars:
        return None
    try:
        return compute_indicator_values(
            bars,
            names=_COCKPIT_SIGNAL_INDICATORS,
            window=window,
        )
    except Exception:  # noqa: BLE001 - frontière fail-safe du cockpit
        return None


def _same_regime_direction(left: str | None, right: str | None) -> bool:
    return left in {"trending_up", "trending_down"} and left == right


def _event_signals(indicators: dict[str, float | None]) -> list[str]:
    signals: list[str] = []
    breakout = _get(indicators, "chart_breakout")
    if breakout is not None and breakout > 0.0:
        signals.append("breakout_up")
    elif breakout is not None and breakout < 0.0:
        signals.append("breakout_down")

    candle_signal = _get(indicators, "candlestick_signal")
    candle = CANDLE_SIGNAL_LABELS.get(candle_signal) if candle_signal is not None else None
    candle_label = _COCKPIT_CANDLE_LABELS.get(candle or "")
    if candle_label is not None:
        signals.append(candle_label)

    z_score = _get(indicators, "z_score")
    if z_score is not None and z_score >= Z_STRETCHED_THRESHOLD:
        signals.append("stretched_up")
    elif z_score is not None and z_score <= -Z_STRETCHED_THRESHOLD:
        signals.append("stretched_down")
    return signals


def multi_horizon_signals(
    bars_by_horizon: dict[str, list[object]],
    *,
    window: int,
    precomputed_indicators_by_horizon: dict[str, dict[str, float | None]] | None = None,
) -> dict:
    """Pré-calcule les signaux cockpit multi-horizon.

    Retourne toujours ``htf`` et ``aligned``. Ajoute ``sig`` seulement si au
    moins un événement notable est détecté. Fonction pure et fail-safe : horizon
    manquant, barres vides ou calcul d'indicateur invalide donnent une sortie
    dégradée, jamais une exception.
    """
    states: dict[str, tuple[dict[str, float | None], MarketRegime]] = {}
    precomputed_indicators_by_horizon = precomputed_indicators_by_horizon or {}
    for horizon in _HORIZON_PRIORITY:
        indicators = precomputed_indicators_by_horizon.get(horizon)
        if indicators is None:
            indicators = _horizon_indicators(bars_by_horizon.get(horizon, []), window=window)
        if indicators is None:
            continue
        try:
            states[horizon] = (indicators, classify_regime(indicators))
        except Exception:  # noqa: BLE001 - frontière fail-safe du cockpit
            continue

    htf_regime = "unknown"
    for horizon in _HORIZON_PRIORITY:
        state = states.get(horizon)
        if state is not None:
            htf_regime = state[1].regime or "unknown"
            break

    base_state = states.get("15m")
    base_regime = base_state[1].regime if base_state is not None else "unknown"
    result = {
        "htf": htf_regime,
        "aligned": _same_regime_direction(base_regime, htf_regime),
    }

    sig: list[str] = []
    for horizon in _HORIZON_PRIORITY:
        state = states.get(horizon)
        if state is None:
            continue
        sig.extend(f"{horizon}:{label}" for label in _event_signals(state[0]))
    if sig:
        result["sig"] = sig
    return result
