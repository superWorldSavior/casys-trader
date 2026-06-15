"""Rotation / hysteresis logic — veille deux niveaux."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from trader.radar import build_radar_snapshot, write_snapshot
from trader.radar_data import CoverageError
from trader.rotation_ledger import log_rotation
from trader.rotation_state import advance_state, load_rotation_state, save_rotation_state, seed_state


def apply_hysteresis(
    ranked: list[dict[str, Any]],
    *,
    current: set[str],
    dwell: dict[str, int],
    cap_m: int,
    delta: float,
    dwell_days: int,
) -> list[str]:
    """Sélection avec hysteresis incumbents + swap conditionnel.

    Args:
        ranked: items triés attractivité desc, chacun avec {'symbol', 'attractiveness'}.
        current: symboles actuellement chauds.
        dwell: nb de jours chaud par symbole.
        cap_m: nombre max de symboles chauds.
        delta: écart minimum d'attractivité pour déclencher un swap.
        dwell_days: nb de jours minimum avant qu'un incumbent soit évictable.

    Returns:
        Liste des symboles sélectionnés (≤ cap_m).
    """
    def attr(symbol: str) -> float:
        for item in ranked:
            if item["symbol"] == symbol:
                return item["attractiveness"]
        return 0.0

    # Incumbents présents dans ranked, triés attractivité desc, tronqués à cap_m
    incumbents_in_ranked = [
        item["symbol"]
        for item in ranked
        if item["symbol"] in current
    ][:cap_m]

    selected: list[str] = list(incumbents_in_ranked)

    # Entrants = non-incumbents dans l'ordre du ranking
    for item in ranked:
        sym = item["symbol"]
        if sym in current:
            continue  # incumbent déjà traité

        if len(selected) < cap_m:
            # slot libre
            selected.append(sym)
        else:
            # chercher l'incumbent évictable le plus faible
            evictable = [
                s for s in selected
                if s in current and dwell.get(s, 0) >= dwell_days
            ]
            if not evictable:
                continue
            weakest = min(evictable, key=attr)
            if item["attractiveness"] > attr(weakest) + delta:
                selected.remove(weakest)
                selected.append(sym)

    return selected


def compose_final(
    *,
    default_hot: list[str],
    sticky: set[str],
    cap_m: int,
) -> tuple[list[str], str | None]:
    """Compose la liste finale : sticky hors quota + non-sticky dans le quota restant.

    Returns:
        (final_list, alert) où alert vaut 'sticky_over_cap' si len(sticky) > cap_m.
    """
    alert: str | None = "sticky_over_cap" if len(sticky) > cap_m else None
    free = max(0, cap_m - len(sticky))

    # sticky triés alphabétiquement pour ordre déterministe, puis non-sticky
    sticky_sorted = sorted(sticky)
    from_default = [s for s in default_hot if s not in sticky][:free]

    # Déduplication ordre préservé
    seen: set[str] = set()
    final: list[str] = []
    for s in sticky_sorted + from_default:
        if s not in seen:
            seen.add(s)
            final.append(s)

    return final, alert


def sticky_symbols(
    *,
    positions: set[str],
    armed_plans: set[str],
    exit_watches: set[str],
    pending_orders: set[str],
) -> set[str]:
    """Union des 4 sets de symboles protégés (hors quota)."""
    return positions | armed_plans | exit_watches | pending_orders


def apply_override(
    *,
    default_hot: list[str],
    add: list[str],
    remove: list[str],
    pool: set[str],
    sticky: set[str],
    free_slots: int,
) -> tuple[list[str], list[dict]]:
    """Applique les overrides agent sur default_hot (liste de symboles non-sticky).

    Rejets machine-readable :
    - retrait d'un s in sticky → {"symbol": s, "reason": "sticky_protected"}
    - ajout d'un s not in pool → {"symbol": s, "reason": "out_of_pool"}
    - ajout dépassant free_slots → {"symbol": s, "reason": "cap_exceeded"}

    Returns:
        (result_list, rejections)
    """
    rejections: list[dict] = []
    result = list(default_hot)

    # Appliquer les retraits
    for s in remove:
        if s in sticky:
            rejections.append({"symbol": s, "reason": "sticky_protected"})
        else:
            if s in result:
                result.remove(s)

    # Appliquer les ajouts
    for s in add:
        if s not in pool:
            rejections.append({"symbol": s, "reason": "out_of_pool"})
        elif len(result) >= free_slots:
            rejections.append({"symbol": s, "reason": "cap_exceeded"})
        else:
            if s not in result:
                result.append(s)

    return result, rejections


class UniverseWriteError(ValueError):
    """Levée quand write_universe_atomic reçoit des données invalides."""


def write_universe_atomic(path: str, symbols: list[str]) -> None:
    """Écrit {"symbols": [...]} dans path de manière atomique via tempfile + os.replace.

    Args:
        path: chemin du fichier de destination.
        symbols: liste non vide de symboles.

    Raises:
        UniverseWriteError: si symbols est vide.
    """
    if not symbols:
        raise UniverseWriteError("symbols ne peut pas être vide")

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump({"symbols": symbols}, f)
        os.replace(tmp_path, path)
    except Exception:
        # Nettoyer le fichier temporaire en cas d'erreur
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def emergency_exits(
    hot_set: set[str],
    ranked: list[dict[str, Any]],
    *,
    emergency_floor: float,
    gap_adverse: frozenset[str] = frozenset(),
    daily_invalidated: frozenset[str] = frozenset(),
) -> set[str]:
    """Retourne les symboles de hot_set à évincer immédiatement.

    Un symbole est évincé si :
    - son attractiveness < emergency_floor (0.0 si absent de ranked) ;
    - il est dans gap_adverse ;
    - il est dans daily_invalidated.
    """
    attr_map: dict[str, float] = {item["symbol"]: item["attractiveness"] for item in ranked}

    return {
        s for s in hot_set
        if attr_map.get(s, 0.0) < emergency_floor
        or s in gap_adverse
        or s in daily_invalidated
    }


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

    # 2. sticky (hors quota) + slots libres
    sticky = sticky_fn()
    free_slots = max(0, cap_m - len(sticky))

    # 3. hystérésis sur les NON-sticky uniquement, cap = free_slots
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
    write_universe_atomic(universe_path, final)
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

    from .rotation_wiring import run_cli

    result = run_cli(args.config_dir, args.state_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
