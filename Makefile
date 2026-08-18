# casys-trader — raccourcis. Lance `make` (ou `make help`) pour la liste.
.DEFAULT_GOAL := help
.PHONY: help watch live once test logs live-logs dash dash-portfolio dash-decisions dash-list macro universe models model-preset storage-report storage-archive storage-schedule storage-unschedule desktop desktop-tauri

help:  ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

watch:  ## Salle de contrôle — voir et piloter le daemon (dashboard + logs + supervision)
	uv run python -m trader.cockpit

desktop:  ## Cockpit read-only dans le navigateur (http://127.0.0.1:1420)
	cd desktop && npm run dev

desktop-tauri:  ## Même UI dans une fenêtre Tauri (optionnel, peut figer)
	cd desktop && npm run tauri dev

live:  ## Moteur en PAPER réel (exécute les ordres simulés, écrit l'état)
	uv run python -m trader.daemon --live

once:  ## Un seul cycle dry-run (test rapide)
	uv run python -m trader.daemon --once

logs:  ## Lit les logs du daemon dans Gonzo (TUI ; touche 'd' → dashboard web Dstl8.Lite). Daemon supposé déjà lancé.
	gonzo -f state/daemon_console.log --follow

live-logs:  ## Démarre le daemon DÉTACHÉ (supervisé, anti-doublon) puis ouvre les logs ; quitter Gonzo NE tue PAS le daemon.
	@uv run python -c "from pathlib import Path; from trader.cockpit.supervisor import launch_daemon as L; r=L(pid_file=Path('state/daemon.pid'), log_file=Path('state/daemon_console.log'), root=Path('.'), status_file=Path('state/daemon_status.json')); print('daemon:', r.reason, '(pid', r.pid, ')')"
	gonzo -f state/daemon_console.log --follow

dash:  ## Régénère tous les dashboards HTML/PNG (index: state/dashboards.html)
	uv run casys-trader dashboards all

dash-portfolio:  ## Régénère seulement les dashboards portefeuille
	uv run casys-trader dashboards portfolio

dash-decisions:  ## Régénère seulement les dashboards décisions
	uv run casys-trader dashboards decisions

dash-list:  ## Liste les dashboards locaux et leurs URLs
	uv run casys-trader dashboards list

MARKET ?= TW
macro:  ## Lance un point macro ciblé — MARKET="TW US GLOBAL" [FORCE=1] ou ALL=1
	uv run casys-trader news-macro refresh $(if $(ALL),--all,$(foreach venue,$(MARKET),--venue $(venue))) $(if $(FORCE),--force,)

universe:  ## Compose la hotlist régionale — MARKET="TW US" [FORCE=1] ou ALL=1 ; FORCE enchaîne le macro du pack courant
	uv run casys-trader universe refresh $(if $(ALL),--all,$(foreach venue,$(MARKET),--venue $(venue))) $(if $(FORCE),--force,)

test:  ## Lance toute la suite de tests
	uv run pytest -q

lint:  ## Lint ruff (imports morts, variables fantômes — config dans pyproject.toml)
	uv run ruff check

check: lint test  ## Lint + tests — le combo pré-commit

ACPX_DIR ?= ../acpx
fork-acpx-build:  ## Construit le fork acpx local (active la rétention native des sessions, voir TRADER_ACPX_BIN)
	cd "$(ACPX_DIR)" && pnpm install --frozen-lockfile && pnpm build

learnings-ingest:  ## Ingestion/scoring/embeddings du store de recall (state/learnings.db)
	uv run python scripts/learnings_ingest.py

models:  ## Jeu de modèles LLM actif (les 5 rôles) et preset courant
	uv run python scripts/model_preset.py show

model-preset:  ## Bascule de preset modèles — dry-run ; PRESET=<nom> [WRITE=1]. Redémarrer le daemon après.
	uv run python scripts/model_preset.py apply $(PRESET) $(if $(WRITE),--write,)

storage-report:  ## Stockage agents : ce qui serait archivé/purgé (simulation, n'écrit rien)
	uv run python -m scripts.archive_agent_storage --older-than $(or $(DAYS),7)

storage-archive:  ## Archive les sessions agents en tar.zst (~50x) et purge les logs codex. DAYS=7 par défaut.
	uv run python -m scripts.archive_agent_storage --older-than $(or $(DAYS),7) --apply

storage-schedule:  ## Installe la rétention hebdo (LaunchAgent, dimanche 04:00)
	plutil -lint ops/launchd/ai.casys.trader.storage-archive.plist
	cp ops/launchd/ai.casys.trader.storage-archive.plist ~/Library/LaunchAgents/
	-launchctl unload ~/Library/LaunchAgents/ai.casys.trader.storage-archive.plist 2>/dev/null
	launchctl load ~/Library/LaunchAgents/ai.casys.trader.storage-archive.plist
	launchctl list | grep casys.trader.storage

storage-unschedule:  ## Désinstalle la rétention hebdo
	-launchctl unload ~/Library/LaunchAgents/ai.casys.trader.storage-archive.plist
	rm -f ~/Library/LaunchAgents/ai.casys.trader.storage-archive.plist
