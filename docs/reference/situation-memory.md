# Référence — Mémoire de situation (bac ②)

> **Type** : Reference (Diátaxis).
> **État vérifié** : 2026-08-16.
> **Code** : `trader/infrastructure/state_db/situation_brief_store.py` ·
> `trader/infrastructure/state_db/situation_memory_store.py` ·
> `trader/application/analyst/news_macro.py` ·
> `trader/runtime/news_macro_runtime.py` ·
> `trader/domain/situation/attribution.py` ·
> `trader/application/analyst/situation_attribution.py`
> **Store** : JSONL canonique `state/news_briefs/` + index dérivé
> `state/situation_memory.db` · **Mesure** : `scripts/situation_note_analytics.py`

Mémoire **périssable, par symbole / famille / zone** : l'analyste news/macro
digère le flux d'events en un brief sourcé ; les points de ce brief sont
recopiés dans un index SQLite reconstructible. Ce n'est **pas** le RAG des
learnings (`learnings.db`) : bac ② de
[l'architecture de connaissance](agent-knowledge-architecture.md), pas bac ③.

## Rôle

Dans le cadre à trois bacs, la situation répond à « que se passe-t-il sur ce
nom ? ». Granularité **par symbole** (et famille / zone), nature **info + état**,
durée de vie **périssable**. La source est le fil d'actu, la macro et
**l'analyste** — pas le trader, pas l'agent univers.

Le brief courant est déjà projeté dans le prompt univers. Cette page documente
le **stockage** des points, leur **scoring marché** (FLAIR) et le digest
comparatif renvoyé à l'analyste. Le retrieval FTS des notes historiques
n'est pas branché (voir [État dormant](#état-dormant)).

## Stores

Deux couches, un seul canon.

### JSONL canonique — `NewsMacroBriefStore`

`trader/infrastructure/state_db/situation_brief_store.py:16-44` gère :

```text
state/news_briefs/YYYY-MM-DD.jsonl       # archive append-only
state/news_briefs/latest-<venue>.jsonl   # cache reconstructible par venue
```

`append()` écrit la ligne du jour puis projette le cache `latest-<venue>`.
`write()` est une façade de compatibilité, jamais un remplacement. Le JSONL
reste la source de vérité ; le `.db` se reconstruit depuis ces fichiers.

Le contrat du brief (zones / familles / symboles / alertes, sources lisibles,
`source_refs` stables) est décrit dans [news.md](news.md) §3.

### Index SQLite dérivé — `SituationMemoryStore`

`trader/infrastructure/state_db/situation_memory_store.py:126-193` ouvre
`state/situation_memory.db` (WAL, `busy_timeout=2000`). Table
`situation_notes` :

| Champ | Rôle |
|---|---|
| `note_key` | clé unique `{brief_id}\|{section_type}\|{section_name}\|{index}` (`situation_memory_store.py:328`) |
| `brief_id`, `brief_ref`, `as_of`, `valid_from`, `valid_until`, `venue` | enveloppe du brief |
| `section_type` | `zone` \| `family` \| `symbol` \| `alert` (`situation_memory_store.py:351-359`) |
| `section_name` | nom de section ; les alertes utilisent `"alerts"` |
| `point_index`, `point` | texte borné du point |
| `source_uuids`, `source_names`, `symbols` | JSON ; `symbols` est une liste |
| `severity`, `signal`, `direction`, `horizon` | copie du point |
| `outcome_score` | score FLAIR (0.0 à l'ingestion) |
| `q_value` | colonne MemRL — **jamais scorée** (insérée `NULL`) |
| `embedding` | BLOB prévu, **jamais écrit** |
| `verdict` | `gagnant` \| `perdant` \| `neutre` \| `non_evaluable` |
| `horizon_sessions` | horizon texte parsé en séances cash (plafond 126) |
| `forward_return`, `coverage_n`, `evaluated_at` | jugement marché |

`direction` stockée = enum du point
(`trader/domain/situation/brief.py:10`) :
`bullish` \| `bearish` \| `risk_on` \| `risk_off` \| `neutral` \| `mixed`.
FLAIR n'évalue que les quatre premières (`directional_action()`,
`attribution.py:419-426`) : `neutral` / `mixed` / vide → `non_evaluable`.

Colonnes d'outcome ajoutées par
`SITUATION_MEMORY_OUTCOME_COLUMNS`
(`trader/infrastructure/state_db/migrations.py:587-593`) :
`verdict`, `horizon_sessions`, `forward_return`, `evaluated_at`, `coverage_n`.

**FTS5** : table virtuelle `situation_notes_fts` sur `point`, content-sync via
triggers AI/AD/AU (`situation_memory_store.py:56-86`). `search()` filtre
optionnellement par `venue`, `symbol`, `family`, `active_at` (TTL
`valid_from` / `valid_until`), limite 1–50, défaut 8.

`ingest_brief()` est idempotent (`INSERT OR IGNORE` sur `note_key`).
L'échec d'indexation est fail-open : le JSONL reste écrit
(`news_macro.py:77-81`).

## Pipeline d'ingestion

```text
LlmNewsMacroAnalyst.analyze()
  → NewsMacroBriefStore.append()
  → SituationMemoryStore.ingest_brief()
```

1. `LlmNewsMacroAnalyst.analyze()`
   (`trader/agent/news_macro/analyzer.py:93`) construit le prompt, appelle le
   routeur, parse et revalide les `source_refs` contre le catalogue d'entrée.
2. `run_news_macro_analysis()`
   (`trader/application/analyst/news_macro.py:57-86`) appelle
   `analyst.analyze()` puis `repository.append()`. Si
   `situation_repository` est fourni, `ingest_brief()` indexe les points.
3. Wiring runtime : `tick_news_macro_analysis()`
   (`trader/runtime/news_macro_runtime.py:243-250` et `:360-367` pour GLOBAL)
   instancie `NewsMacroBriefStore(state/news_briefs)` et
   `SituationMemoryStore(state/situation_memory.db)`, puis passe les deux
   au use-case. Le runner `NewsMacroAnalysisRunner` est async, single-flight.

L'analyste ne sélectionne aucun symbole. Détail du brief et des gates de
fraîcheur : [news.md](news.md) §3.

## Attribution FLAIR

Livrée (commit `9fa7920`). Le marché juge la **note**, pas le trader : un
appel `bullish` sur une famille est vrai ou faux selon le mouvement
ultérieur de ce panier, qu'un trade ait été pris ou non.

### Domaine — `trader/domain/situation/attribution.py`

- `parse_horizon_sessions()` (`attribution.py:311`) : texte libre → nombre de
  séances cash (unités `d/j/w/s/m/q/t`, mots, dates calendaires, bornes
  qualitatives). Vide, illisible, ou `> MAX_HORIZON_SESSIONS` (126,
  `attribution.py:25`) → `None` (`non_evaluable`). Un intervalle garde la
  borne haute.
- `directional_action()` (`attribution.py:419`) : `bullish`/`risk_on` → `BUY` ;
  `bearish`/`risk_off` → `SELL` ; le reste → `None`.
- `targets_for_note()` (`attribution.py:452`) : famille → panier catalogue ; symbole →
  `section_name` ; alerte → champ `symbols` ; **zone hors scope** (tuple
  vide).
- `equal_weight_forward_return()` : close-to-close équipondéré des membres
  qui ont un chemin complet.
- `classify_note_quality()` / `to_flair_verdict()` : `gagnant`/`perdant`/
  `neutre` puis mapping WIN/LOSS pour le moteur FLAIR partagé
  (`compute_outcome_scores`, `shrinkage_k=5.0`).

### Application — `trader/application/analyst/situation_attribution.py`

- `evaluate_note()` (`situation_attribution.py:87`) : parse horizon, résout
  les cibles, calcule le forward, classe. Direction non scorable, pas de
  cibles, ou horizon illisible → `non_evaluable` (persisté). Forward encore
  incomplet → skip (`None`, réessayable).
- `persist_and_score()` (`situation_attribution.py:215`) : `apply_outcomes()`
  écrit `verdict`, `horizon_sessions`, `forward_return`, `evaluated_at`,
  `coverage_n` (`situation_memory_store.py:282-306`) ; puis
  `update_outcome_scores()` écrit `outcome_score` **sans toucher**
  `q_value` (`situation_memory_store.py:308-317`).

### Déclenchement — sync background fail-open + secours opérateur

```text
uv run python scripts/situation_note_analytics.py evaluate
```

Le `LearningSyncRunner` appelle `refresh_situation_outcomes()` dans
`run_learning_sync()` ; le script opérateur reste disponible en secours.
Le daemon utilise le même `get_bars` throttlé que l'univers, batch
`DEFAULT_OUTCOME_BATCH_SIZE`. Les notes immatures
(`evaluate_note()` → `None`) ne sont pas figées. Opt-out :
`TRADER_SITUATION_OUTCOMES_ENABLED=0`. Un échec de cette étape est
loggé en warning et n'interrompt jamais le cycle.

## État dormant — à lire noir sur blanc

1. **Retrieval runtime non branché.** `SituationMemoryStore.search()`
   existe (`situation_memory_store.py:195`). Aucun agent (trader, univers,
   analyste) ne l'appelle. Ce n'est donc pas un RAG de situation de bout
   en bout. Le brief *courant* est projeté ; les notes *historiques* ne
   le sont pas.
2. **`q_value` n'est jamais scorée.** La colonne est dans le DDL
   (`situation_memory_store.py:51`). L'ingestion l'insère à `NULL`
   (`situation_memory_store.py:347`). `update_outcome_scores()` ne la
   met pas à jour. MemRL situation n'est pas implémenté.
3. **Les verdicts remontent en digest comparatif, pas en liste de notes.**
   `situation_feedback_digest()` agrège directions / `section_type` /
   familles (`n>=5`) et le runtime l'injecte dans le prompt news/macro
   sous `situation_feedback` (`role: comparative_context_not_directives`).
   Champ omis si l'échantillon est insuffisant ou si le store est
   indisponible. Jamais de liste de symboles ni de directive.

`embedding` est dans le même état que `q_value` : prévu, jamais rempli.

## Mesure

`scripts/situation_note_analytics.py` :

| Commande | Effet |
|---|---|
| `summary` (défaut) | lit les notes déjà jugées, imprime le résumé JSON |
| `evaluate` | juge via `DataSource` (YFinance par défaut), écrit les verdicts, recalcule FLAIR, imprime le même résumé |

Le résumé (`summarize_outcomes()`, `situation_attribution.py:323`) ventile
par `direction`, `family`, `horizon_bucket` (`seance` / `court` /
`semaines` / `mois` / `trimestre` / `long`) et `section_type`, avec
win-rate, forward moyen, `outcome_score` moyen, et listes `pays` /
`decoit`. Réévaluer une note déjà jugée **écrase** le verdict ; ça ne
duplique rien. `q_value` n'est pas touchée.

## Invariant

Le `.db` est un **dérivé reconstructible**. Ne jamais le traiter comme
source de vérité : ce sont les JSONL `state/news_briefs/`. Même discipline
que `learnings.db` (cf. [learnings-rag.md](learnings-rag.md), Architecture
§11).

## Voir aussi

- [Architecture de connaissance](agent-knowledge-architecture.md) (bac ②)
- [News, challengers et analyste](news.md)
- [Learnings & RAG](learnings-rag.md) (bac ③, pile distincte)
- [Données macro](macro.md)
