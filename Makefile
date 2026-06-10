# casys-trader — raccourcis. Lance `make` (ou `make help`) pour la liste.
.DEFAULT_GOAL := help
.PHONY: help watch live once test

help:  ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

watch:  ## Salle de contrôle — voir et piloter le daemon (dashboard + logs + supervision)
	uv run python -m trader.cockpit

live:  ## Moteur en PAPER réel (exécute les ordres simulés, écrit l'état)
	uv run python -m trader.daemon --live

once:  ## Un seul cycle dry-run (test rapide)
	uv run python -m trader.daemon --once

test:  ## Lance toute la suite de tests
	uv run pytest -q
