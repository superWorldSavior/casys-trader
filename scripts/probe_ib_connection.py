"""Probe : connexion IB Gateway (paper, port 4002) + récupération de barres.

Valide le flux IB AVANT de construire le connecteur derrière `market.get_bars`.
Teste un stock (SPY) et un forex (EURUSD) — deux sec_type du mapping
config/ib_contracts.yaml — pour confirmer qu'on récupère bien des barres.

Lancer Gateway en paper, API socket activée sur 4002, « Allow localhost ».
Au 1er connect, le Gateway peut demander d'accepter la connexion entrante.
"""

from __future__ import annotations

import sys

HOST, PORT, CLIENT_ID = "127.0.0.1", 4002, 17


def main() -> None:
    try:
        from ib_async import IB, Forex, Stock
    except ImportError:
        print("ib_async non installé (uv add ib_async)")
        sys.exit(1)

    ib = IB()
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"CONNECT FAIL: {type(e).__name__}: {e}")
        print("→ Gateway: Configure > Settings > API > Enable Socket Clients, "
              "port 4002, Allow connections from localhost. Accepter la connexion entrante au 1er connect.")
        sys.exit(1)
    print(f"CONNECTÉ — serverVersion={ib.client.serverVersion()} accounts={ib.managedAccounts()}")

    # Données différées si pas de souscription temps réel (3 = delayed, 4 = delayed-frozen)
    ib.reqMarketDataType(3)

    def fetch(label: str, contract, what: str, duration: str = "2 D") -> None:
        try:
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract, endDateTime="", durationStr=duration,
                barSizeSetting="15 mins", whatToShow=what,
                useRTH=False, formatDate=2,
            )
            last = bars[-1] if bars else None
            print(f"{label}: {len(bars)} barres ; dernière = "
                  + (f"{last.date} O={last.open} H={last.high} L={last.low} C={last.close}" if last else "AUCUNE"))
        except Exception as e:  # noqa: BLE001
            print(f"{label}: FAIL {type(e).__name__}: {e}")

    fetch("SPY (STK/SMART/USD)", Stock("SPY", "SMART", "USD"), "TRADES")
    fetch("EURUSD (CASH/IDEALPRO)", Forex("EURUSD"), "MIDPOINT", duration="1 D")

    ib.disconnect()
    print("OK déconnecté")


if __name__ == "__main__":
    main()
