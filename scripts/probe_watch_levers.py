"""Probe A/B : le mode batch sous-utilise-t-il les leviers d'auto-régulation ?

Question : pourquoi l'agent live ne pose JAMAIS d'indicator_watch ni n'allonge
son next_wake_in_minutes ? Hypothèse : le contrat de sortie batch
(`_BATCH_FINAL_CONTRACT`) expose `indicator_watch:<object|null>` SANS schéma,
alors que le contrat single (`_COMPACT_OUTPUT_CONTRACT`) en décrit le format
complet. À contexte IDENTIQUE, on compare l'usage des leviers en single vs batch.

Déterministe côté contexte (cockpit synthétique fixe, données « fraîches »,
setup z élevé / ER bas — le scénario exact des learnings BTC). Le LLM échantillonne,
donc on lance plusieurs symboles pour avoir un taux, pas un point.

RÉSULTAT (2026-06-08) : watches=0/4 single ET 0/4 batch ; next_wake=4/4 les deux.
=> Hypothèse « le contrat batch ampute le schéma watch » RÉFUTÉE : même en single
avec le schéma complet, l'agent ne pose pas de watch. Il régule sa cadence via le
levier scalaire `next_wake_in_minutes`, pas via l'objet structuré `indicator_watch`.

LIMITE connue : le bloc SINGLE est bruité. `decide()` attend un contexte
MONO-symbole ; on lui passe ici le cockpit de tout l'univers, donc l'agent ignore
le `symbol` demandé et répond ce qu'il veut (souvent « SPY »). Le signal robuste
(0 watch / next_wake systématique) tient car cohérent sur single ET batch ; ne
PAS se fier à la colonne `symbol`/`action` du bloc single.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from trader import codex_client
from trader.agent_context import build_market_cockpit
from trader.tools.market import Bar

# Petit sous-ensemble : single = 1 appel/symbole, batch = 1 appel total.
SYMBOLS = ["BTC-USD", "SPY", "NVDA", "GC=F"]


def impulse_bars(symbol: str) -> list[Bar]:
    """Barres bruitées (ER bas) avec un spike récent (z élevé) — setup 'tentant'."""
    seed = sum(ord(c) for c in symbol)
    base = 50.0 + seed % 200
    bars = []
    for i in range(60):
        noise = math.sin((seed + i) * 1.7) * 3.0  # bruit -> efficiency_ratio bas
        px = base + noise
        if i >= 57:  # impulsion isolée sur les 3 dernières barres -> z_score élevé
            px += 12.0
        bars.append(Bar(ts=f"t{i}", open=px, high=px + 1.5, low=px - 1.5, close=px, volume=1000.0 + i))
    return bars


def shared_context() -> dict:
    bars = {s: impulse_bars(s) for s in SYMBOLS}
    prices = {s: bars[s][-1].close for s in SYMBOLS}
    cockpit = build_market_cockpit(bars, symbols=SYMBOLS, prices=prices, window=48)
    return {
        "cockpit": cockpit,
        "stale_market_data": [],  # données FRAÎCHES : pas de court-circuit garde
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "positions": {}},
        "kpis": {"return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": None, "n_trades": 0},
        "attribution": {"round_trips": [], "by_confidence": {}, "by_exit_reason": {}},
        "learnings": [],  # vide : on veut tester l'émergence des leviers, pas la copie
    }


def lever_summary(d: codex_client.Decision) -> dict:
    has_watch = isinstance(getattr(d, "indicator_watch", None), dict)
    return {
        "symbol": d.symbol,
        "action": d.action,
        "next_wake": d.next_wake_in_minutes,
        "indicator_watch": "YES" if has_watch else "no",
        "watch_keys": sorted(d.indicator_watch.keys()) if has_watch else [],
    }


def main() -> None:
    mandate = Path("mandate/mandate.md").read_text(encoding="utf-8") if Path("mandate/mandate.md").exists() else ""
    memory = Path("mandate/memory.md").read_text(encoding="utf-8") if Path("mandate/memory.md").exists() else ""
    ctx = shared_context()

    print(f"[probe] contexte: {len(SYMBOLS)} symboles, données fraîches, setup z↑/ER↓\n")

    # --- A) SINGLE : un appel par symbole (contrat avec schéma watch complet) ---
    print("=== A) SINGLE (decide, _COMPACT_OUTPUT_CONTRACT — schéma watch présent) ===")
    single = []
    for sym in SYMBOLS:
        resp = codex_client.decide(
            symbol=sym, mandate=mandate, memory=memory, context=ctx,
            allow_context_request=True, timeout_s=120,
        )
        if isinstance(resp, codex_client.Decision):
            single.append(lever_summary(resp))
            print(f"  {lever_summary(resp)}")
        else:
            print(f"  {sym}: REQUEST_CONTEXT (next_wake={resp.next_wake_in_minutes})")

    # --- B) BATCH : un seul appel pour tous (contrat sans schéma watch) ---
    print("\n=== B) BATCH (decide_batch, _BATCH_FINAL_CONTRACT — schéma watch ABSENT) ===")
    batch = codex_client.decide_batch(
        symbols=SYMBOLS, mandate=mandate, memory=memory,
        shared_context=ctx, per_symbol={s: {} for s in SYMBOLS},
        allow_context_request=False, timeout_s=180,
    )
    batch_rows = []
    for sym in SYMBOLS:
        resp = batch[sym]
        if isinstance(resp, codex_client.Decision):
            batch_rows.append(lever_summary(resp))
            print(f"  {lever_summary(resp)}")
        else:
            print(f"  {sym}: REQUEST_CONTEXT")

    # --- Verdict ---
    def rate(rows, key, pred):
        n = sum(1 for r in rows if pred(r))
        return f"{n}/{len(rows)}" if rows else "0/0"

    print("\n=== VERDICT ===")
    print(f"  watches posées   single={rate(single, 'w', lambda r: r['indicator_watch']=='YES')}"
          f"   batch={rate(batch_rows, 'w', lambda r: r['indicator_watch']=='YES')}")
    print(f"  next_wake fixé    single={rate(single, 'n', lambda r: r['next_wake'] is not None)}"
          f"   batch={rate(batch_rows, 'n', lambda r: r['next_wake'] is not None)}")
    print(json.dumps({"single": single, "batch": batch_rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
