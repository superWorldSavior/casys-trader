# Référence — Learnings & RAG (recall outcome-weighted)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/learnings/` (store, consolidator, embeddings) · **Outil LLM** : `recall_learnings` (`agent/tools/learnings`)
> L'ancien package `trader.learnings.*` reste compatible via alias virtuel.
> **Store** : `state/learnings.db` (SQLite) · **Spec** : `docs/superpowers/specs/2026-07-02-learnings-recall-design.md`

Mémoire de trading **pondérée par le résultat** : les notes passées sont scorées
(FLAIR) et rappelées par similarité sémantique au moment de décider. Rien ne se
jette.

## Store — `agent/learnings/store` (SQLite)

`learnings.db` est un **dérivé reconstructible** — les JSONL d'archives restent
canoniques. Mode **WAL** : lecture concurrente (daemon) + écriture (`record_recall`)
sans blocage.

| Table | Rôle |
|---|---|
| `notes` | notes complètes : texte, symbole, verdict, `outcome_score` (FLAIR), `embedding` (BLOB float32 LE) |
| `recalls` | trace des notes **servies par décision** (phase ②, lien note↔decision) |

### Scoring FLAIR — `compute_outcome_scores(shrinkage_k=5.0)`
Sur les notes WIN/LOSS seulement :
```
lift(note)      = (1.0 si WIN, sinon 0.0) − base_rate            # normalisé par symbole
outcome_score   = lift / (1 + shrinkage_k)                       # n=1 en V1 → shrinkage vers 0
```
Shrinkage bayésien : à faible volume, l'`outcome_score` est tiré vers 0 (prudence).

### Ingestion & embeddings
- `ingest_jsonl(path, *, source)` — ingère les notes d'archive.
- `backfill_embeddings(embedder)` — calcule les embeddings manquants en batch.

## Embeddings — `agent/learnings/embeddings`
Client OpenAI minimal (`embed_texts`), modèle `text-embedding-3-small`. Fail-safe.
Les vecteurs sont **pré-calculés** et stockés en BLOB dans `notes.embedding`.

## Recall (RAG) — outil `recall_learnings`
Au moment de décider, le LLM peut appeler `recall_learnings` (domain tool, cf.
[agent-tools](agent-tools.md)). La recherche (`store.search`) est **hybride** :
FTS5 BM25 (mots-clés) **+** cosinus vectoriel (si un embedding de la requête est
fourni), fusionnés par **Reciprocal Rank Fusion** (k=60), puis re-scorés :

```
final_score = rrf + outcome_score + freshness    # freshness = exp(−age_j/τ) − 1, τ=30j
```

`outcome_score` est **additionné** (pas un multiplicateur), et une décroissance de
**fraîcheur** pénalise les notes anciennes. Latence typique 2-8 ms.

**Le tracing dans `recalls` est fait POST-décision**, pas par le handler de l'outil :
`decision_recorder` extrait les `note_ids` de la trace `tool_calls` et appelle
`store.record_recall`. Le `decision_id` (frappé depuis `cycle_ts` + séquence)
n'existe pas encore au moment de l'appel outil.

## Consolidateur — `agent/learnings/consolidator`
`ConsolidatedLearningsStore` : consolide les notes brutes en synthèses
(`select_new_raw` depuis un watermark, `normalize_consolidated`). Réinjecté au
contexte LLM (continuité de mémoire longue).

## Invariant
Le `.db` est reconstructible depuis les archives ; ne jamais le traiter comme
source de vérité canonique (ce sont les JSONL). Cf. Architecture §11.

## Voir aussi
- [Domain tools](agent-tools.md) (`recall_learnings`) · [reporting](reporting.md) (perf).
