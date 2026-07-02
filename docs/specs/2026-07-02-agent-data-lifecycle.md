# Cycle de vie des données de l'agent — état des lieux et évolutions

**Date** : 2026-07-02
**Statut** : état des lieux documenté ; décisions d'évolution à trancher (§6)
**Source** : inventaire multi-agents (4 lecteurs + critique de complétude) +
vérifications manuelles sur les findings critiques.

## 1. Vue d'ensemble — trois familles, trois cycles de vie

Réponse à la confusion « décisions structurées vs learnings » : ce sont bien
des choses distinctes, avec des politiques opposées.

| Famille | Fichiers | Politique | Verdict |
|---|---|---|---|
| **Ledger analytique** (audit, attribution, qualité) | `decisions.jsonl`, `events.jsonl`, `history.jsonl`, `model_performance.jsonl`, `rotation_ledger.jsonl` | append-only, **AUCUNE rétention** | ⚠️ le problème est ici |
| **Mémoire de l'agent** (réinjectée au prompt) | `learnings.jsonl` (rolling 200), `learnings_consolidated.json` (≤10 global + ≤5/symbole), `mandate/memory.md` | bornée par design | ✅ seule famille avec une vraie politique |
| **Snapshots d'état** (écrasés en place) | `broker.json`, `current_report.json`, `scheduler.json`, `trade_plans.json`, `venue_state.json`, `daemon_status.json`, `rotation_state.json`… | taille stable | ✅ sains |

Le lien entre les deux premières : le champ `learning` d'une décision LLM est
persisté DEUX fois — dans la row du ledger (audit) et dans `learnings.jsonl`
(mémoire). Le ledger garde tout ; la mémoire évince au-delà de 200 bruts et le
consolidateur compacte périodiquement (watermark + quotas).

## 2. Les accumulateurs — chiffres au 2026-07-02

| Fichier | Taille | Croissance/jour | Rétention | Lecteurs chauds |
|---|---|---|---|---|
| `decisions.jsonl` | **81 Mo** / 18 491 rows | 3,5–13 Mo (~775-1 874 rows) | aucune | tail-50 TUI (sain) ; **full-scan à chaque append (§3.1)** |
| `events.jsonl` | 11 Mo / 90 449 | ~0,5 Mo (~4 400 lignes) | aucune | cockpit incrémental (sain) ; full-scan on-demand seulement |
| `history.jsonl` | 271 Ko | ~11 Ko | aucune | full-scan ×2 toutes les 2 s (TUI) — OK aujourd'hui |
| `model_performance.jsonl` | 104 Ko | ~5 Ko | aucune | full-scan ×2 toutes les 2 s (TUI, sans cache partagé) |
| `radar_cache/` | **29 Mo** | ~2 Mo/jour de bourse | aucune | rotation (jour courant seulement) |
| `backtest_cache/` | 7,3 Mo / 247 fichiers | dormant | aucune éviction | backtest on-demand |
| `~/.acpx/sessions/` | **1,3 Go** / 489 fichiers | ~2 sessions/appel acpx | **aucun TTL** | acpx uniquement |

Projection `decisions.jsonl` au rythme actuel : ~200 Mo fin juillet, >1 Go fin
2026. Projection `~/.acpx/sessions` : croît avec chaque review Codex et chaque
consolidation.

## 3. Bugs de performance vérifiés (P0)

### 3.1 Dédup d'append = relecture intégrale du ledger — VÉRIFIÉ

`trader/decision_ledger.py:167` : `append()` appelle `_existing_ids()` →
`_read_rows()` → `path.read_text()` **sur les 81 Mo, à chaque décision
persistée** (~1 200-1 800/jour) ≈ 90+ Go d'IO séquentielle/jour, en croissance
quadratique (taille × fréquence). Le daemon paie ce coût dans `run_cycle`.

**Fix** : mémoriser le set d'IDs dans l'instance (`self._ids`, chargé
paresseusement une fois, entretenu à l'append). ~10 lignes + test. Le store vit
dans le process daemon ; le seul autre écrivain (CLI seed) est off-line.

### 3.2 `decision_audit.json` (1,3 Mo) désérialisé à chaque cycle

`trader/meta_performance.py:18` fait `json.loads(path.read_text())` à chaque
`run_cycle` (`daemon.py:1890`) alors que le fichier ne change qu'aux runs
manuels de `casys audit`. **Fix** : cache par `st_mtime`. ~6 lignes.

### 3.3 `replace_all()` non atomique

`trader/decision_ledger.py:249` réécrit le ledger entier via `write_text()`
sans tmp + `os.replace` ni backup : un crash pendant `backfill_code_versions`
corrompt le ledger. **Fix** : écriture atomique (pattern déjà utilisé par les
snapshots du repo).

## 4. Risques de rétention (P1)

1. **`decisions.jsonl` sans rotation** — tous les lecteurs *de production*
   sont bornés (tail-50 TUI) ou on-demand (audit, decision-quality, bench).
   Rien n'exige le fichier monolithique : une rotation mensuelle
   (`state/archive/decisions-YYYY-MM.jsonl.gz`) est transparente pour le
   daemon si les outils d'analyse apprennent à lire `archive/*` (glob déjà
   pratiqué : `cli.py` agrège `state_archive_*/events.jsonl`).
2. **`events.jsonl`** — même traitement, même mécanique.
3. **`radar_cache/`** — purge des fichiers > N jours (30 ?) au tick de
   rotation. Nettoyer aussi le fichier aberrant
   `2026-06-15T05:30:28…json` (bug historique : clé de cache avec composant
   horaire, jamais réutilisée).
4. **`~/.acpx/sessions` (1,3 Go)** — hors repo mais c'est NOTRE usage
   (consolidateur + reviews). `acpx sessions prune --older-than 30
   --include-history` périodique (cible make + rappel, ou cron).
5. **Learnings : perte silencieuse possible** — si la consolidation échoue
   longtemps et que le backlog dépasse 200 bruts, les plus anciens sont
   évincés sans trace. Mitigation simple : alerte TUI si
   `pending_learnings > 150` (le compteur existe déjà).

## 5. Déchets identifiés (nettoyage one-shot sans risque)

- `last_decision_bench*_*.json` : ~2,5 Mo, 8 fichiers figés du 12/06, aucun
  lecteur en prod.
- `daemon_console.20260630-110854.pre-logclean.log` : 5,4 Mo, orphelin d'une
  troncature manuelle.
- 12 fichiers `.bak-*` manuels (`broker.json.bak-pre-fx-*`,
  `decisions.jsonl.bak-2026-06-16-preincident` 8,4 Mo,
  `history.jsonl.bak-precliff-fix`…) : dead artifacts jamais lus par le code.
- `mandate/memory.md` : `Memory.append_learning()` (`trader/tools/memory.py:94`)
  n'a AUCUN appelant en prod — code mort à supprimer ou brancher.
- `rotation_ledger.jsonl` : write-only (aucun lecteur) — à garder comme
  audit-trail (c'est l'archive des univers) mais le documenter comme tel.

Proposition : déplacer les `.bak-*` et les bench files dans
`state_archive_2026-07-cleanup/` (déjà gitignoré) plutôt que supprimer.

## 6. Décisions à trancher

| # | Décision | Options | Reco |
|---|---|---|---|
| D-a | Fix dédup append (§3.1) | cache in-memory / index latéral / rien | **cache in-memory, immédiat** |
| D-b | Cache mtime `decision_audit.json` (§3.2) | oui / non | **oui, immédiat** |
| D-c | Rotation `decisions.jsonl` + `events.jsonl` | mensuelle gzip vers `state/archive/` / SQLite / rien | **mensuelle gzip** (SQLite = YAGNI tant que ça tient) |
| D-d | Dédup schéma des rows (18+ champs top-level dupliqués de `decision{}`) | schema v2 (−40 % taille) / laisser | **coupler à la rotation** (les archives gzippent bien la redondance ; v2 seulement si D-c ne suffit pas) |
| D-e | Purges caches (`radar_cache` 30 j, acpx sessions 30 j) | auto au tick / cible make manuelle | **auto pour radar_cache, make + rappel pour acpx** |
| D-f | Nettoyage one-shot §5 | archive / suppression | **archive** |

## 7. Hors périmètre noté

- `state/scheduler.json`, `trade_plans.json` : snapshots sains, purgés par
  leur propriétaire.
- Cache yfinance (`~/Library/Caches/py-yfinance`, 92 Ko) : géré par la lib.
- Doubles lectures TUI sans cache (`model_performance` ×2, `consolidated` ×2
  par cycle) : optimisation opportuniste, pas urgente à ces tailles.
