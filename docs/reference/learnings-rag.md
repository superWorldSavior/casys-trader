# Référence — Learnings & RAG (recall outcome-weighted)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/learnings/` (store, consolidator, embeddings) · **Outil LLM** : `recall_learnings` (`agent/tools/learnings`)
> L'ancien package `trader.learnings.*` reste compatible via alias virtuel.
> **Store** : `state/learnings.db` (SQLite) · **Spec** : `docs/superpowers/specs/2026-07-02-learnings-recall-design.md`

Mémoire de trading **pondérée par le résultat** : les notes passées sont scorées
(FLAIR), rappelées au moment de décider et réévaluées selon l'utilité observée de
leurs rappels (MemRL). Rien ne se jette.

## Store — `agent/learnings/store` (SQLite)

`learnings.db` est un **dérivé reconstructible** — les JSONL d'archives restent
canoniques. Mode **WAL** : lecture concurrente (daemon) + écriture (`record_recall`)
sans blocage.

| Table | Rôle |
|---|---|
| `notes` | notes complètes : texte, symbole, verdict, `outcome_score` (FLAIR), `q_value`/`q_updates` (MemRL), `embedding` |
| `recalls` | notes **servies par décision**, puis verdict, reward et forward return différés |

### Scoring FLAIR — `compute_outcome_scores(shrinkage_k=5.0)`
Sur les notes WIN/LOSS seulement :
```
lift(note)      = (1.0 si WIN, sinon 0.0) − base_rate            # normalisé par symbole
outcome_score   = lift / (1 + shrinkage_k)                       # n=1 en V1 → shrinkage vers 0
```
Shrinkage bayésien : à faible volume, l'`outcome_score` est tiré vers 0 (prudence).

### Maintenance automatique

`LearningSyncRunner` tourne en arrière-plan après les cycles, en single-flight :

1. ingestion idempotente de `learnings-from-ledger.jsonl`,
   `learnings-evicted.jsonl` et `learnings.jsonl` ;
   le bootstrap historique ne remplit que les verdicts encore absents ;
2. embeddings manquants en micro-lots de 64 ;
3. outcomes matures, à 1 jour avec fallback 4 heures, en lots de 128 ;
4. recalcul FLAIR et application des rewards MemRL aux notes effectivement
   rappelées.

Le worker est **best-effort et fail-open** : aucune erreur d'embedding, de données
ou de SQLite ne bloque une décision. Son dernier état est observable dans
`state/learnings_sync_status.json`. `TRADER_LEARNINGS_AUTO_SYNC_ENABLED=0` permet
de désactiver seulement cette maintenance. `make learnings-ingest` reste un outil
manuel de reconstruction/backfill.

## Embeddings — `agent/learnings/embeddings`
Client OpenAI minimal (`embed_texts`), modèle `text-embedding-3-small`. Fail-safe.
Les vecteurs sont **pré-calculés** et stockés en BLOB dans `notes.embedding`.

## Recall (RAG) — push borné + outil `recall_learnings`

Chaque décision LLM reçoit automatiquement au plus **deux** expériences :
recherche facettée du symbole, puis fallback de famille. Ce petit push local ne
fait aucun appel d'embedding et ne remplace jamais la situation fraîche du titre.
L'agent peut ensuite appeler `recall_learnings` pour une analogie de setup plus
précise. La recherche (`store.search`) est **hybride** :
FTS5 BM25 (mots-clés) **+** cosinus vectoriel (si un embedding de la requête est
fourni), fusionnés par **Reciprocal Rank Fusion** (k=60), puis re-scorés :

```
q_confidence = q_updates / (q_updates + 5)
final_score  = rrf + outcome_score + freshness
               + 0.2 × q_value × q_confidence × exp(−age_j/τ)
```

`outcome_score` est **additionné** (pas un multiplicateur), et une décroissance de
**fraîcheur** pénalise les notes anciennes. Latence typique 2-8 ms.

**Le tracing dans `recalls` est fait POST-décision**, pour le push automatique
comme pour l'outil : `decision_recorder` extrait les `note_ids` de la trace et
appelle `store.record_recall`. Les décisions synthétiques sans appel modèle ne
créent pas cette attribution.

## MemRL — reward différée

Une fois la décision mature, le worker classe son résultat avec le même moteur
que la qualité de décision puis applique aux notes rappelées :

```
reward = +1 (WIN), 0 (NEUTRAL), -1 (LOSS)
Q_new  = Q_old + 0.1 × (reward − Q_old)
```

`evaluated_at` rend l'update idempotent. `UNKNOWN` ferme la trace sans modifier
Q. La contribution de Q au ranking est shrinkée par `q_updates` : une expérience
ne devient donc pas dominante après un seul outcome.

## Consolidateur — `agent/learnings/consolidator`
`ConsolidatedLearningsStore` : consolide les notes brutes en synthèses
(`select_new_raw` depuis un watermark, `normalize_consolidated`). En queue, la
projection pousse seulement les principes `global` et les guardrails ; les slots
`by_symbol` restent hors prompt et l'expérience ciblée passe par FLAIR.

## Invariant
Le `.db` est reconstructible depuis les archives ; ne jamais le traiter comme
source de vérité canonique (ce sont les JSONL). Cf. Architecture §11.

## Voir aussi
- [Domain tools](agent-tools.md) (`recall_learnings`) · [reporting](reporting.md) (perf).
