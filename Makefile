# casys-trader — raccourcis. Lance `make` (ou `make help`) pour la liste.
.DEFAULT_GOAL := help
.PHONY: help watch live once test logs live-logs

help:  ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

watch:  ## Salle de contrôle — voir et piloter le daemon (dashboard + logs + supervision)
	uv run python -m trader.cockpit

live:  ## Moteur en PAPER réel (exécute les ordres simulés, écrit l'état)
	uv run python -m trader.daemon --live

once:  ## Un seul cycle dry-run (test rapide)
	uv run python -m trader.daemon --once

logs:  ## Lit les logs du daemon dans Gonzo (TUI ; touche 'd' → dashboard web Dstl8.Lite). Daemon supposé déjà lancé.
	gonzo -f state/daemon_console.log --follow

live-logs:  ## Démarre le daemon DÉTACHÉ (supervisé, anti-doublon) puis ouvre les logs ; quitter Gonzo NE tue PAS le daemon.
	@uv run python -c "from pathlib import Path; from trader.cockpit_supervisor import launch_daemon as L; r=L(pid_file=Path('state/daemon.pid'), log_file=Path('state/daemon_console.log'), root=Path('.'), status_file=Path('state/daemon_status.json')); print('daemon:', r.reason, '(pid', r.pid, ')')"
	gonzo -f state/daemon_console.log --follow

test:  ## Lance toute la suite de tests
	uv run pytest -q

lint:  ## Lint ruff (imports morts, variables fantômes — config dans pyproject.toml)
	uv run ruff check

check: lint test  ## Lint + tests — le combo pré-commit

fork-acpx-build:  ## Construit le fork acpx local (active la rétention native des sessions, voir TRADER_ACPX_BIN)
	cd /Users/erwanpesle/Documents/GitHub/acpx && pnpm install --frozen-lockfile && pnpm build
