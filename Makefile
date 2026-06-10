# casys-trader — raccourcis. Lance `make` (ou `make help`) pour la liste.
.DEFAULT_GOAL := help
.PHONY: help tui demo daemon live once test stats attrib logs kill unkill

help:  ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

tui:  ## Dashboard live (lecture seule) — Ctrl+C pour quitter
	uv run python -m trader.tui

demo:  ## Aperçu du dashboard avec données d'exemple (sans moteur)
	uv run python scripts/tui_demo.py

daemon:  ## Moteur en DRY-RUN continu (log les ordres, ne mute rien)
	uv run python -m trader.daemon

live:  ## Moteur en PAPER réel (exécute les ordres simulés, écrit l'état)
	uv run python -m trader.daemon --live

once:  ## Un seul cycle dry-run (test rapide)
	uv run python -m trader.daemon --once

test:  ## Lance toute la suite de tests
	uv run pytest -q

stats:  ## KPI live (rendement, drawdown, sharpe…)
	uv run python -m trader.stats

attrib:  ## Attribution décision→résultat (P&L par trade, calibration)
	uv run python -m trader.attribution

logs:  ## Viewer humain des logs machine (live tail, JSONL joli) — toolong
	uvx --from toolong tl state/events.jsonl state/decisions.jsonl

kill:  ## Stop d'urgence : crée le fichier KILL (zéro ordre)
	touch KILL

unkill:  ## Retire le kill-switch
	rm -f KILL
