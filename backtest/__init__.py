"""backtest — rejeu maison de l'agent sur l'historique (SimBroker + yfinance).

Rejeu ÉCHANTILLONNÉ : on ne rejoue pas barre par barre (un appel Codex par barre
serait ruineux), mais à une cadence choisie, pour itérer vite et pas cher.

Modules :
- data.py    : chargement historique + accès as-of (sans lookahead)
- engine.py  : boucle de rejeu (decision_fn injectable, réutilise SimBroker/RiskGate)
- metrics.py : KPI + rendu CLI
"""
