"""One-off: génère config/symbol_names.yaml = {symbole: nom société} pour le pool.

Source : yfinance .info (longName, repli shortName). Déterministe une fois figé :
le TUI lit ce yaml, aucun appel réseau au rendu. Symbole absent → repli ticker brut.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    pool = yaml.safe_load((ROOT / "config" / "pool.yaml").read_text(encoding="utf-8"))
    symbols = list(dict.fromkeys(pool.get("symbols") or []))
    out_path = ROOT / "config" / "symbol_names.yaml"

    names: dict[str, str] = {}
    if out_path.exists():  # reprise idempotente
        prev = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        if isinstance(prev, dict):
            names.update({str(k): str(v) for k, v in prev.items()})

    for i, sym in enumerate(symbols, 1):
        if sym in names:
            continue
        try:
            info = yf.Ticker(sym).info
            name = info.get("longName") or info.get("shortName")
            if name:
                names[sym] = str(name).strip()
                print(f"[{i}/{len(symbols)}] {sym} -> {names[sym]}", flush=True)
            else:
                print(f"[{i}/{len(symbols)}] {sym} -> (aucun nom)", flush=True)
        except Exception as exc:  # noqa: BLE001 — best-effort, on continue
            print(f"[{i}/{len(symbols)}] {sym} -> ERREUR {exc}", flush=True)
        if i % 5 == 0:  # écriture incrémentale + politesse réseau
            out_path.write_text(
                yaml.safe_dump(names, allow_unicode=True, sort_keys=True),
                encoding="utf-8",
            )
        time.sleep(0.4)

    out_path.write_text(
        yaml.safe_dump(names, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    print(f"OK — {len(names)}/{len(symbols)} noms écrits dans {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
