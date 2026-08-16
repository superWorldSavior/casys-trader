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

Le même moteur FLAIR note les sélections d'univers, **par base** (`allocation`
contre le banc, `direction` signée) : voir
[D16](../superpowers/specs/2026-08-16-universe-selection-bench-verdict-design.md).

### Maintenance automatique

`LearningSyncRunner` tourne en arrière-plan après les cycles, en single-flight :

1. ingestion idempotente de `learnings-from-ledger.jsonl`,
   `rationale-experiences.jsonl`, `learnings-evicted.jsonl` et
   `learnings.jsonl` ;
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

## Recall (RAG) — outil `recall_learnings`

L'expérience historique reste **pull-only** : l'agent appelle
`recall_learnings` pour une analogie de setup précise. Elle n'est jamais poussée
automatiquement dans le cockpit, et ne remplace jamais la situation fraîche du
titre. La recherche (`store.search`) est **hybride** :
FTS5 BM25 (mots-clés) **+** cosinus vectoriel (si un embedding de la requête est
fourni), fusionnés par **Reciprocal Rank Fusion** (k=60), puis re-scorés :

```
q_confidence = q_updates / (q_updates + 5)
final_score  = rrf + outcome_score + freshness
               + 0.2 × q_value × q_confidence × exp(−age_j/τ)
```

`outcome_score` est **additionné** (pas un multiplicateur), et une décroissance de
**fraîcheur** pénalise les notes anciennes. Latence typique 2-8 ms.

**Le tracing dans `recalls` est fait POST-décision** pour les seuls appels
explicites à l'outil : `decision_recorder` extrait les `note_ids` de la trace et
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
`ConsolidatedLearningsStore` conserve au plus dix règles globales. Le
consolidateur reçoit un pool borné : 50 learnings nouveaux/modifiés, 15
confirmations FLAIR historiques et 15 contre-exemples, chacun avec son feedback
(`pending` ou verdict, rendement, score FLAIR et Q de note). Il ne produit pas
de règle par symbole.

Chaque règle persistée contient `rule_id`, `note`, `robustness`,
`evidence_note_ids` et un résumé d'évidence calculé par le code. Une règle
retenue/reformulée garde son ID ; une nouvelle règle le laisse vide et reçoit un
ID stable après validation. Une provenance qui n'appartient pas au pool, ou un
ID de règle inventé, invalide la consolidation. `high` requiert trois preuves
évaluées, plus de WIN que de LOSS et une reward moyenne positive.

Le cockpit ne reçoit que `{rule_id, note, robustness}` avec les guardrails. Les
fichiers historiques `by_symbol` restent lisibles pour compatibilité, mais toute
nouvelle consolidation écrit ce slot vide. Les règles globales citées via
`applied_learning_ids` ont leur MemRL séparé des notes rappelées par l'outil :
seules les règles réellement citées reçoivent l'outcome différé de la décision.

### Capture, ingestion et outcomes

Chaque décision réellement écrite par le LLM devient automatiquement une
expérience lorsque les trois conditions suivantes sont vraies :

- `decision_source=llm` et `model_called=true` ;
- la rationale est non vide et exploitable ;
- aucun `llm_error` ne signale une réponse invalide.

Les `HOLD` synthétiques d'infrastructure, les décisions sans appel modèle et les
rationales de fallback ne créent donc pas de note. `record_learning` reste
offert à l'agent, mais il n'ouvre pas une deuxième mémoire : son texte est une
annotation optionnelle fusionnée à la rationale de la même décision. Il n'existe
qu'une note par `decision_id`, dédupliquée dès le JSONL brut puis dans SQLite.

La note est ingérée immédiatement dans `learnings.db` avec un feedback
`pending`, donc rappelable par FTS sans attendre l'embedding. La vectorisation,
FLAIR et MemRL restent best-effort en arrière-plan. Le rattrapage historique lit
le ledger décisions, applique le même filtre et écrit les expériences absentes
dans `state/archive/rationale-experiences.jsonl` ; il est dry-run par défaut.

Une entrée (`OPEN_LONG`, `OPEN_SHORT`, `SCALE_IN`, ou nouvelle jambe d'un
`FLIP`) n'est évaluée qu'après la clôture complète de son lot FIFO, sorties
partielles et commissions incluses. `HOLD`, ordre bloqué, `CLOSE` et `REDUCE`
conservent le jugement contrefactuel à 1 jour, avec fallback 4 heures. Pour les
notes aplaties, le worker rejoint le `decision_id` au ledger avant le scoring :
il retrouve ainsi le `portfolio_snapshot` requis pour juger correctement un
`HOLD`. Les anciennes notes à identifiant synthétique restent sur l'horizon fixe.

Une consolidation est due à 50 nouvelles notes, 10 feedbacks modifiés, ou au
rattrapage quotidien d'un changement en attente. Les révisions exactes envoyées
au modèle ne sont marquées curées qu'après une écriture réussie : un échec ne
perd donc ni une note ni un outcome arrivé pendant l'appel.

Un événement avec `new_raw_count=0` n'est pas nécessairement vide : il peut
correspondre à un `feedback refresh` ou à un `daily catch-up` qui recure un pool
de candidats existants après évolution de leurs outcomes. Le libellé opérateur
doit être lu avec `curation_due` et `curated_candidate_count`, pas avec le seul
compteur de notes brutes.

## Invariant
Le `.db` est reconstructible depuis les archives ; ne jamais le traiter comme
source de vérité canonique (ce sont les JSONL). Cf. Architecture §11.

## Voir aussi
- [How-to maintenance](../how-to/maintain-learnings.md) ·
  [Domain tools](agent-tools.md) (`recall_learnings`) ·
  [reporting](reporting.md) (perf) ·
  [D16 sélections vs banc](../superpowers/specs/2026-08-16-universe-selection-bench-verdict-design.md).
