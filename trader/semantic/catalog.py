"""Governed semantic catalog for trading indicators.

This is the local TraderNexus layer: a small metadata graph describing what the
agent may query. It is intentionally not MCP-specific; the CLI and daemon import
the same Python functions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

IndicatorKind = Literal["level", "family", "timeframe", "lookback", "window", "indicator"]

LEVELS = ["market", "family", "symbol", "timeframe", "lookback", "window", "as_of", "indicator"]

WINDOWS = [2, 3, 5, 12, 24, 32, 48, 96, 120, 240]
AS_OF_MODES = ["latest"]

INDICATOR_LABEL_VALUES: dict[str, dict[str, float]] = {
    "chart_breakout": {"breakout_up": 1.0, "breakout_down": -1.0},
    "candlestick_signal": {
        "bullish_engulfing": 1.0,
        "bull_engulf": 1.0,
        "bearish_engulfing": -1.0,
        "bear_engulf": -1.0,
        "hammer": 0.5,
        "shooting_star": -0.5,
    },
}


def label_to_value(indicator: str, label: str) -> float | None:
    return INDICATOR_LABEL_VALUES.get(indicator, {}).get(label)


TIMEFRAMES: dict[str, dict] = {
    "15m": {
        "label": "15 minutes",
        "style": "intraday",
        "source_interval": "15m",
        "lookbacks": ["1d", "5d"],
        "default_lookback": "5d",
        "default_window": 32,
    },
    "30m": {
        "label": "30 minutes",
        "style": "intraday",
        "source_interval": "30m",
        "lookbacks": ["5d", "1mo"],
        "default_lookback": "5d",
        "default_window": 48,
    },
    "1h": {
        "label": "1 hour",
        "style": "intraday",
        "source_interval": "1h",
        "lookbacks": ["5d", "1mo", "3mo"],
        "default_lookback": "5d",
        "default_window": 48,
    },
    "4h": {
        "label": "4 hours",
        "style": "swing_intraday",
        "source_interval": "1h",
        "aggregation": "4x1h",
        "lookbacks": ["5d", "1mo", "3mo"],
        "default_lookback": "1mo",
        "default_window": 48,
    },
    "1d": {
        "label": "1 day",
        "style": "daily",
        "source_interval": "1d",
        "lookbacks": ["1mo", "3mo", "6mo", "1y"],
        "default_lookback": "6mo",
        "default_window": 120,
    },
}

FAMILIES: dict[str, list[str]] = {
    "indices": ["SPY", "QQQ", "DIA"],
    "countries": ["EWT", "EWQ"],
    "europe_indices": ["^FCHI"],
    "defense": ["ITA", "HO.PA", "AM.PA", "RHM.DE"],
    "energy": ["XLE", "USO", "UNG"],
    "commodities_futures": ["CL=F", "BZ=F", "NG=F"],
    "metals": ["GC=F"],
    "nasdaq_single_names": ["NVDA", "AAPL"],
    # -------------------------------------------------------------------------
    # Univers US large-cap sectorisé (S&P 500) — découpage GICS.
    # Préfixe us_ pour isoler le sous-graphe dans le cross-asset family_regime.
    # Tickers sans suffixe (Yahoo Finance US).
    # -------------------------------------------------------------------------
    # Technology
    "us_tech": [
        "MSFT",   # Microsoft
        "GOOGL",  # Alphabet (Google)
        "META",   # Meta Platforms
        "AMD",    # Advanced Micro Devices
    ],
    # Communication Services
    "us_communication": [
        "NFLX",   # Netflix
        "DIS",    # Walt Disney
        "T",      # AT&T
        "VZ",     # Verizon Communications
    ],
    # Financials
    "us_financials": [
        "JPM",    # JPMorgan Chase
        "BAC",    # Bank of America
        "GS",     # Goldman Sachs
        "BLK",    # BlackRock
    ],
    # Healthcare
    "us_healthcare": [
        "JNJ",    # Johnson & Johnson
        "UNH",    # UnitedHealth Group
        "PFE",    # Pfizer
        "ABBV",   # AbbVie
    ],
    # Energy
    "us_energy": [
        "XOM",    # ExxonMobil
        "CVX",    # Chevron
        "COP",    # ConocoPhillips
        "SLB",    # Schlumberger (SLB)
    ],
    # Consumer Discretionary
    "us_consumer_disc": [
        "AMZN",   # Amazon
        "TSLA",   # Tesla
        "HD",     # Home Depot
        "MCD",    # McDonald's
    ],
    # Consumer Staples
    "us_consumer_staples": [
        "PG",     # Procter & Gamble
        "KO",     # Coca-Cola
        "WMT",    # Walmart
        "PEP",    # PepsiCo
    ],
    # Industrials
    "us_industrials": [
        "CAT",    # Caterpillar
        "GE",     # GE Aerospace
        "HON",    # Honeywell
        "UPS",    # United Parcel Service
    ],
    # Materials
    "us_materials": [
        "LIN",    # Linde
        "APD",    # Air Products & Chemicals
        "FCX",    # Freeport-McMoRan (copper)
        "NEM",    # Newmont (gold mining)
    ],
    # Utilities
    "us_utilities": [
        "NEE",    # NextEra Energy
        "DUK",    # Duke Energy
        "SO",     # Southern Company
        "AEP",    # American Electric Power
    ],
    # Real Estate
    "us_real_estate": [
        "PLD",    # Prologis (logistics REIT)
        "AMT",    # American Tower (cell towers REIT)
        "EQIX",   # Equinix (data centers REIT)
        "SPG",    # Simon Property Group (retail REIT)
    ],
    # -------------------------------------------------------------------------
    # Univers taïwanais d'Erwan — découpage sectoriel best-effort, à valider.
    # Source : 30 tickers TWSE (.TW) / TPEx (.TWO) du pool papier.
    # Préfixe tw_ pour isoler le sous-graphe dans le cross-asset family_regime (D2).
    # Les tickers incertains sont regroupés dans tw_other — voir rapport de commit.
    # -------------------------------------------------------------------------
    # Fonderies (pure-play wafer fab)
    "tw_foundry": ["2330.TW", "2303.TW"],  # TSMC, UMC
    # Wafer silicium (matière première semi)
    "tw_wafer": ["6488.TWO"],  # GlobalWafers
    # Mémoire / DRAM
    "tw_memory": ["2408.TW"],  # Nanya Technology
    # IC Design / Fabless
    "tw_ic_design": [
        "2454.TW",   # MediaTek
        "2379.TW",   # Realtek Semiconductor
        "3661.TW",   # Parade Technologies
        "8299.TWO",  # Phison Electronics (NAND controllers)
        "3443.TW",   # Global Mixed-Mode Technology (power ICs)
    ],
    # OSAT — assemblage & test
    "tw_osat": [
        "3711.TW",   # ASE Technology Holding
        "2449.TW",   # King Yuan Electronics (KYEC)
        "2383.TW",   # Elite Semiconductor Memory Technology (ESMT)
        "3081.TWO",  # Walton Advanced Engineering
    ],
    # EMS / ODM (assemblage systèmes)
    "tw_ems_odm": [
        "2317.TW",   # Foxconn / Hon Hai Precision
        "2382.TW",   # Quanta Computer
        "3231.TW",   # Wistron NeWeb Corp (modules comm / IoT)
    ],
    # Composants passifs & énergie
    "tw_components": [
        "2327.TW",   # Yageo (résistances/condensateurs)
        "2492.TW",   # Walsin Technology (composants passifs)
        "2308.TW",   # Delta Electronics (alimentations, thermique)
        "2301.TW",   # Lite-On Technology (composants, LED)
    ],
    # PCB / substrats
    "tw_pcb": [
        "3189.TW",   # Kinsus Interconnect Technology (substrats IC)
        "8046.TW",   # Nan Ya PCB
        "3532.TW",   # Taiwan PCB Techvest
        "3037.TW",   # Unimicron Technology (PCB avancés)
        "2368.TW",   # Gold Circuit Electronics (PCB multicouches)
    ],
    # Thermique / refroidissement (heatsinks, heatpipes, vapor chambers)
    "tw_thermal": [
        "3017.TW",   # Asia Vital Components (AVC) — refroidissement serveurs/AI
        "3324.TWO",  # Auras Technology — modules de refroidissement CPU/GPU/VGA
    ],
    # Connectique & composants optiques/RF
    "tw_connectors": [
        "6442.TW",   # EZconn Corporation — connecteurs RF + composants fibre optique
    ],
    # Équipements de fab semi (wet process, nettoyage wafer)
    "tw_equipment": [
        "3131.TWO",  # Grand Process Technology — équipements wet process (wafer cleaning, wet bench)
    ],
    # Services d'ingénierie fab semi (salles blanches, MEP, construction)
    "tw_services": [
        "2404.TW",   # United Integrated Services (UIS) — construction salles blanches & MEP fabs TSMC
    ],
    # Secteur incertain — à reclasser après validation
    "tw_other": [],
    "crypto": ["BTC-USD"],
    "forex_majors": [
        "EURUSD=X",
        "GBPUSD=X",
        "USDJPY=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "NZDUSD=X",
        "EURJPY=X",
    ],
}


@dataclass(frozen=True)
class IndicatorSpec:
    name: str
    label: str
    description: str
    category: str
    inputs: list[str]
    output: str
    concepts: list[str]


INDICATORS: tuple[IndicatorSpec, ...] = (
    IndicatorSpec(
        name="return",
        label="Window return",
        description="Close-to-close return over the requested window.",
        category="momentum",
        inputs=["close"],
        output="decimal",
        concepts=["momentum", "performance", "trend"],
    ),
    IndicatorSpec(
        name="volatility",
        label="Return volatility",
        description="Population standard deviation of close-to-close returns.",
        category="risk",
        inputs=["close"],
        output="decimal",
        concepts=["volatility", "risk", "noise"],
    ),
    IndicatorSpec(
        name="ohlc_volatility",
        label="OHLC volatility proxy",
        description="Garman-Klass style volatility proxy using open/high/low/close.",
        category="risk",
        inputs=["open", "high", "low", "close"],
        output="decimal",
        concepts=["volatility", "risk", "ohlc"],
    ),
    IndicatorSpec(
        name="z_score",
        label="Price z-score",
        description="Latest close versus the rolling close distribution.",
        category="mean_reversion",
        inputs=["close"],
        output="standard_deviation",
        concepts=["mean reversion", "extreme", "range"],
    ),
    IndicatorSpec(
        name="efficiency_ratio",
        label="Kaufman efficiency ratio",
        description="Net price displacement divided by total absolute path length.",
        category="regime",
        inputs=["close"],
        output="0..1",
        concepts=["regime", "trend", "chop", "efficiency"],
    ),
    IndicatorSpec(
        name="autocorrelation",
        label="Lag-1 return autocorrelation",
        description="Autocorrelation of consecutive returns in the requested window.",
        category="regime",
        inputs=["close"],
        output="-1..1",
        concepts=["regime", "momentum", "mean reversion"],
    ),
    IndicatorSpec(
        name="relative_strength",
        label="Family relative strength",
        description="Symbol return minus its family average return.",
        category="cross_asset",
        inputs=["close", "family"],
        output="decimal",
        concepts=["cross asset", "relative strength", "family"],
    ),
    IndicatorSpec(
        name="spread_zscore",
        label="Family spread z-score",
        description="Latest symbol-minus-peers close spread versus its rolling spread distribution.",
        category="cross_asset",
        inputs=["close", "family"],
        output="standard_deviation",
        concepts=["cross asset", "spread", "relative value", "mean reversion"],
    ),
    IndicatorSpec(
        name="candlestick_signal",
        label="Candlestick reversal signal",
        description="Compact Japanese candlestick score: bullish engulfing/hammer positive, bearish engulfing/shooting star negative.",
        category="candlestick",
        inputs=["open", "high", "low", "close"],
        output="-1..1",
        concepts=["candlestick", "japanese candles", "reversal", "price action"],
    ),
    IndicatorSpec(
        name="candle_body_ratio",
        label="Candle body ratio",
        description="Absolute candle body divided by high-low range for the latest bar.",
        category="candlestick",
        inputs=["open", "high", "low", "close"],
        output="0..1",
        concepts=["candlestick", "body", "price action", "conviction"],
    ),
    IndicatorSpec(
        name="candle_wick_skew",
        label="Candle wick skew",
        description="Lower wick minus upper wick divided by high-low range; positive means lower rejection.",
        category="candlestick",
        inputs=["open", "high", "low", "close"],
        output="-1..1",
        concepts=["candlestick", "wick", "rejection", "price action"],
    ),
    IndicatorSpec(
        name="chart_breakout",
        label="Chart breakout",
        description="Latest close above prior rolling high (+1), below prior rolling low (-1), or inside range (0).",
        category="chart_pattern",
        inputs=["high", "low", "close"],
        output="-1|0|1",
        concepts=["chart", "breakout", "support", "resistance", "price action"],
    ),
    IndicatorSpec(
        name="trend_slope",
        label="Normalized trend slope",
        description="Linear regression slope of closes normalized by average close.",
        category="chart_pattern",
        inputs=["close"],
        output="decimal",
        concepts=["chart", "trendline", "trend", "slope"],
    ),
    IndicatorSpec(
        name="range_position",
        label="Rolling range position",
        description="Latest close location inside rolling low-high range; 0 support side, 1 resistance side.",
        category="chart_pattern",
        inputs=["high", "low", "close"],
        output="0..1",
        concepts=["chart", "support", "resistance", "range"],
    ),
)


def family_for_symbol(symbol: str) -> str | None:
    for family, symbols in FAMILIES.items():
        if symbol in symbols:
            return family
    return None


def list_indicators() -> list[dict]:
    return [asdict(indicator) for indicator in INDICATORS]


def find_indicators(concept: str) -> list[dict]:
    needle = concept.lower().strip()
    if not needle:
        return list_indicators()
    return [
        asdict(indicator)
        for indicator in INDICATORS
        if needle in (
            " ".join([
                indicator.name,
                indicator.label,
                indicator.description,
                indicator.category,
                *indicator.concepts,
            ]).lower()
        )
    ]


def normalize_temporal_query(
    *,
    timeframe: str | None = None,
    lookback: str | None = None,
    window: int | None = None,
    as_of: str | None = None,
) -> dict:
    selected_timeframe = str(timeframe or "1h")
    if selected_timeframe not in TIMEFRAMES:
        selected_timeframe = "1h"
    spec = TIMEFRAMES[selected_timeframe]

    selected_lookback = str(lookback or spec["default_lookback"])
    if selected_lookback not in spec["lookbacks"]:
        selected_lookback = str(spec["default_lookback"])

    default_window = int(spec["default_window"])
    try:
        selected_window = int(window if window is not None else default_window)
    except (TypeError, ValueError):
        selected_window = default_window
    selected_window = min(max(selected_window, min(WINDOWS)), max(WINDOWS))

    selected_as_of = str(as_of or "latest")
    if selected_as_of not in AS_OF_MODES:
        selected_as_of = "latest"

    return {
        "timeframe": selected_timeframe,
        "source_interval": str(spec["source_interval"]),
        "lookback": selected_lookback,
        "window": selected_window,
        "as_of": selected_as_of,
    }


def describe_semantic_layer() -> dict:
    return {
        "revision": "seed",
        "transport": "local_cli_and_python_imports",
        "levels": LEVELS,
        "families": FAMILIES,
        "timeframes": TIMEFRAMES,
        "windows": WINDOWS,
        "as_of_modes": AS_OF_MODES,
        "indicators": list_indicators(),
        "query_shapes": {
            "indicator_get": {
                "axes": ["symbol", "indicator", "timeframe", "lookback", "window", "as_of"],
                "symbol": "ticker, e.g. SPY",
                "timeframe": list(TIMEFRAMES),
                "lookback": "provider period allowed by timeframe",
                "window": WINDOWS,
                "as_of": AS_OF_MODES,
                "names": [indicator.name for indicator in INDICATORS],
            },
            "indicator_compare": {
                "axes": ["family", "indicator", "timeframe", "lookback", "window", "as_of"],
                "family": list(FAMILIES),
                "timeframe": list(TIMEFRAMES),
                "lookback": "provider period allowed by timeframe",
                "window": WINDOWS,
                "as_of": AS_OF_MODES,
                "metrics": [indicator.name for indicator in INDICATORS],
            },
        },
    }
