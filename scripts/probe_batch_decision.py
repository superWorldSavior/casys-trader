"""Probe: Spark rend-il N décisions structurées propres en UN seul exec batch ?

Décide la viabilité du batch stateless (option C). Construit un cockpit
synthétique déterministe sur l'univers réel (21 symboles), demande un array de
décisions, et inspecte: JSON parseable, len == N, clés requises par élément.
"""

from __future__ import annotations

import json
import math
import time

import yaml

from trader.agent import llm
from trader.agent.context import build_market_cockpit
from trader.tools.market import Bar

REQUIRED = {"symbol", "action", "quantity", "confidence", "rationale"}


def synth_bars(symbol: str) -> list[Bar]:
    seed = sum(ord(c) for c in symbol)
    base = 50.0 + seed % 200
    bars = []
    for i in range(60):
        drift = math.sin((seed + i) / 7.0) * 2.0
        px = base + drift + i * 0.05
        bars.append(Bar(ts=f"t{i}", open=px, high=px + 1, low=px - 1, close=px, volume=1000.0 + i))
    return bars


def main() -> None:
    symbols = yaml.safe_load(open("config/universe.yaml"))["symbols"]
    bars_by_symbol = {s: synth_bars(s) for s in symbols}
    prices = {s: bars_by_symbol[s][-1].close for s in symbols}
    cockpit = build_market_cockpit(bars_by_symbol, symbols=symbols, prices=prices, window=48)

    contract = (
        "Tu es l'agent décideur d'un système de trading paper. On te donne le "
        "cockpit compact de TOUT l'univers. Rends une décision pour CHAQUE symbole.\n"
        "Réponds UNIQUEMENT par un objet JSON: {\"decisions\": [ "
        '{"symbol": "<SYM>", "action": "BUY|SELL|HOLD", "quantity": <number>, '
        '"confidence": <0..1>, "rationale": "<court>", '
        '"intent": "OPEN_LONG|OPEN_SHORT|REDUCE|CLOSE|REVERSE|HOLD", '
        '"learning": <string|null>} ] }\n'
        "Un élément par symbole, dans l'ordre du cockpit. Pas de texte autour.\n\n"
        f"# Cockpit\n{json.dumps(cockpit, ensure_ascii=False)}\n"
    )

    print(f"[probe] n_symbols={len(symbols)} prompt_chars={len(contract)}")
    backend = llm.AcpxBackend(model=llm.DEFAULT_SPARK_MODEL)
    t0 = time.time()
    result = backend.complete(contract, timeout_s=180)
    dt = time.time() - t0
    print(f"[probe] elapsed={dt:.1f}s type={type(result).__name__}")
    if isinstance(result, llm.LlmFailure):
        print(f"[probe] FAILURE code={result.code} msg={result.message[:300]}")
        return

    text = result.text
    print(f"[probe] response_chars={len(text)}")
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1])
    except Exception as e:  # noqa: BLE001
        print(f"[probe] JSON PARSE FAIL: {e}")
        print(text[:1000])
        return

    decisions = data.get("decisions", [])
    print(f"[probe] parsed decisions={len(decisions)} / expected={len(symbols)}")
    got_syms = [d.get("symbol") for d in decisions]
    missing = [s for s in symbols if s not in got_syms]
    bad = [d.get("symbol") for d in decisions if not REQUIRED.issubset(d.keys())]
    print(f"[probe] missing_symbols={missing}")
    print(f"[probe] elements_missing_required_keys={bad}")
    verdict = (
        len(decisions) == len(symbols) and not missing and not bad
    )
    print(f"[probe] VERDICT={'OK — batch viable' if verdict else 'DEGRADE — batch risque'}")


if __name__ == "__main__":
    main()
