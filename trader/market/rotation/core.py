"""Rotation / hysteresis logic — veille deux niveaux."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from trader.domain.universe.selection import (
    apply_hysteresis,
    apply_override,
    compose_final,
    emergency_exits,
    sticky_symbols as sticky_symbols,
)
from trader.domain.universe.user_overrides import apply_user_overrides
from trader.infrastructure.files.universe_config import (
    UniverseWriteError as UniverseWriteError,
    load_user_overrides,
    write_universe_atomic as write_universe_atomic,
)
from trader.market.radar import build_radar_snapshot, write_snapshot
from trader.market.radar_data import CoverageError
from trader.market.rotation.ledger import log_rotation
from trader.market.rotation.state import advance_state, load_rotation_state, save_rotation_state, seed_state


def run(
    *,
    config_dir: str,
    state_dir: str,
    as_of: str,
    rank_fn: Any,
    sticky_fn: Any,
    override_fn: Any,
    pool: set[str],
    cap_m: int,
    delta: float,
    dwell_days: int,
    emergency_floor: float,
    gap_adverse: frozenset[str] = frozenset(),
    daily_invalidated: frozenset[str] = frozenset(),
) -> dict:
    """Orchestration EOD de la rotation d'univers.

    Les I/O réseau/agent sont injectées via rank_fn, sticky_fn, override_fn.

    Returns:
        dict avec clés : final_hot_set, default_hot_set, alerts, written.
    """
    state = load_rotation_state(state_dir)
    universe_path = str(Path(config_dir) / "universe.yaml")
    ledger_path = Path(state_dir) / "rotation_ledger.jsonl"
    alerts: list[str] = []

    # B. Bootstrap : si état vide ET universe.yaml présent → seed depuis universe.yaml
    _is_empty_state = (
        state["current_hot_set"] == []
        and state["dwell_days_by_symbol"] == {}
        and state["last_valid_universe"] == []
    )
    if _is_empty_state:
        _universe_yaml_path = Path(config_dir) / "universe.yaml"
        if _universe_yaml_path.exists():
            try:
                _universe_content = yaml.safe_load(_universe_yaml_path.read_text(encoding="utf-8"))
                _universe_symbols = _universe_content.get("symbols", [])
                if _universe_symbols:
                    state = seed_state(_universe_symbols)
            except Exception:
                pass  # garder l'état vide si lecture échoue

    # 1. Ranking — peut lever CoverageError -> fail-safe : on NE réécrit PAS l'univers
    try:
        rank_result = rank_fn()
        ranked = rank_result["ranked"]
        # Le dict prime sur le param gap_adverse
        gap_adverse = rank_result.get("gap_adverse", gap_adverse)
    except CoverageError:
        log_rotation(
            ledger_path,
            as_of=as_of,
            default_hot=set(state["current_hot_set"]),
            final_hot=set(state["last_valid_universe"]),
            sticky=set(),
            overrides={},
            rejects=[],
            alerts=["coverage_insufficient"],
        )
        return {
            "final_hot_set": state["last_valid_universe"],
            "default_hot_set": state["current_hot_set"],
            "alerts": ["coverage_insufficient"],
            "written": False,
        }

    # 2. sticky réellement hors quota : cap_m porte uniquement sur les choisis.
    sticky = sticky_fn()
    free_slots = cap_m

    # 3. hystérésis sur les NON-sticky uniquement, cap = cap_m
    ranked_ns = [r for r in ranked if r["symbol"] not in sticky]
    current_ns = {s for s in state["current_hot_set"] if s not in sticky}
    default_hot = apply_hysteresis(
        ranked_ns,
        current=current_ns,
        dwell=state["dwell_days_by_symbol"],
        cap_m=free_slots,
        delta=delta,
        dwell_days=dwell_days,
    )

    # 4. sortie d'urgence
    evicted = emergency_exits(
        set(default_hot),
        ranked,
        emergency_floor=emergency_floor,
        gap_adverse=gap_adverse,
        daily_invalidated=daily_invalidated,
    )
    default_hot = [s for s in default_hot if s not in evicted]

    # 5. override agent — échec => commit du défaut
    overrides: dict[str, list] = {"add": [], "remove": []}
    rejects: list[dict] = []
    try:
        overrides = override_fn({"ranked": ranked, "default_hot": list(default_hot)}) or {"add": [], "remove": []}
        hot, rejects = apply_override(
            default_hot=default_hot,
            add=overrides.get("add", []),
            remove=overrides.get("remove", []),
            pool=pool,
            sticky=sticky,
            free_slots=free_slots,
        )
    except Exception:
        hot = default_hot
        alerts.append("override_unavailable")

    # 6. compose final (sticky hors quota)
    final, cap_alert = compose_final(default_hot=hot, sticky=sticky, cap_m=cap_m)
    if cap_alert:
        alerts.append(cap_alert)

    # A. No-leader : si final est vide, ne pas écrire l'univers mais persister rotation_at
    if not final:
        alerts.append("no_leader")
        log_rotation(
            ledger_path,
            as_of=as_of,
            default_hot=set(default_hot),
            final_hot=set(state["last_valid_universe"]),
            sticky=sticky,
            overrides=overrides,
            rejects=rejects,
            alerts=alerts,
        )
        # Persiste last_rotation_at sans toucher hot-set ni dwell
        no_leader_state = {**state, "last_rotation_at": as_of}
        save_rotation_state(state_dir, no_leader_state)
        return {
            "final_hot_set": state["last_valid_universe"],
            "default_hot_set": default_hot,
            "alerts": alerts,
            "written": False,
        }

    # D. Snapshot radar — si rank_fn fournit ineligible + components_by_symbol
    _ineligible = rank_result.get("ineligible")
    _components = rank_result.get("components_by_symbol")
    if _ineligible is not None and _components is not None:
        snapshot = build_radar_snapshot(
            ranked,
            _ineligible,
            as_of=as_of,
            components_by_symbol=_components,
        )
        write_snapshot(Path(state_dir), snapshot)

    # 7. écriture atomique + état + ledger
    # C. advance_state avec last_valid=final (hot non-sticky uniquement dans current_hot_set)
    # Pin/ban cockpit appliqués aussi sur ce chemin legacy (parité avec venues.tick)
    user_overrides = load_user_overrides(universe_path)
    final = apply_user_overrides(
        final, pin=user_overrides.pin, ban=user_overrides.ban, sticky=sticky
    )
    if final:
        write_universe_atomic(universe_path, final)
    # final vidé par un ban → on garde le fichier (fusible non-vide) ;
    # le ban reste effectif via la lecture daemon (effective_universe_symbols).
    save_rotation_state(state_dir, advance_state(state, hot, last_valid=final, rotation_at=as_of))
    log_rotation(
        ledger_path,
        as_of=as_of,
        default_hot=set(default_hot),
        final_hot=set(final),
        sticky=sticky,
        overrides=overrides,
        rejects=rejects,
        alerts=alerts,
    )
    return {
        "final_hot_set": final,
        "default_hot_set": default_hot,
        "alerts": alerts,
        "written": True,
    }


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI — rotation EOD complète.

    Usage:
      rotation.py --run --config-dir CONFIG --state-dir STATE
    """
    parser = argparse.ArgumentParser(description="Rotation EOD orchestrator")
    parser.add_argument("--run", action="store_true", help="Lancer la rotation EOD")
    parser.add_argument("--config-dir", default="config", help="Répertoire de config")
    parser.add_argument("--state-dir", default="state", help="Répertoire d'état")
    args = parser.parse_args(argv)

    if not args.run:
        parser.print_help()
        return 0

    from trader.runtime.market_rotation_runtime import run_cli

    result = run_cli(args.config_dir, args.state_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
