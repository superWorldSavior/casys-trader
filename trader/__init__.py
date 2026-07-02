"""casys-trader — infrastructure de l'agent de trading autonome.

Le daemon (boucle runtime) appelle Codex pour décider, passe par le risk gate,
exécute en paper. La stratégie / les indicateurs / le calendrier de réveil ne
sont PAS codés ici : ils sont définis par l'agent via le mandat et la mémoire.
"""

from __future__ import annotations

import importlib
import sys

__version__ = "0.1.0"

_COMPAT_MODULES = {
    "decision_audit": "trader.reporting.decision_audit",
    "decision_bench": "trader.reporting.decision_bench",
    "decision_ledger": "trader.reporting.decision_ledger",
    "family_regime": "trader.market.family_regime",
    "features": "trader.market.features",
    "fx": "trader.market.fx",
    "fx_rates": "trader.market.fx_rates",
    "gross_priority": "trader.market.gross_priority",
    "macro_calendar": "trader.market.macro_calendar",
    "macro_series": "trader.market.macro_series",
    "meta_performance": "trader.reporting.meta_performance",
    "palette": "trader.ui.palette",
    "pool_config": "trader.config.pool",
    "portfolio_config": "trader.config.portfolio",
    "process_env": "trader.runtime.process_env",
    "radar": "trader.market.radar",
    "radar_config": "trader.market.radar_config",
    "radar_data": "trader.market.radar_data",
    "regime": "trader.market.regime",
    "rotation_bench": "trader.rotation.bench",
    "rotation_collectors": "trader.rotation.collectors",
    "rotation_daemon": "trader.rotation.daemon",
    "rotation_ledger": "trader.rotation.ledger",
    "rotation_override": "trader.rotation.override",
    "rotation_schedule": "trader.rotation.schedule",
    "rotation_state": "trader.rotation.state",
    "rotation_venues": "trader.rotation.venues",
    "rotation_wiring": "trader.rotation.wiring",
    "tool_trace": "trader.reporting.tool_trace",
    "cockpit_events": "trader.cockpit.events",
    "cockpit_supervisor": "trader.cockpit.supervisor",
}

for _old_name, _new_name in _COMPAT_MODULES.items():
    _module = importlib.import_module(_new_name)
    globals()[_old_name] = _module
    sys.modules[f"{__name__}.{_old_name}"] = _module

del _module, _new_name, _old_name
