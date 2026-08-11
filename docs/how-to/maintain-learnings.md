# How-to — Maintenir les learnings, FLAIR et MemRL

> **Type** : How-to (Diátaxis) — procédure opérateur.
> **Sources canoniques** : JSONL sous `state/` et `state/archive/` ·
> **Dérivé de recall** : `state/learnings.db` · **État worker** :
> `state/learnings_sync_status.json`.

La maintenance automatique est best-effort et ne bloque jamais le trading. Les
JSONL restent la source de vérité ; `learnings.db` est un index SQLite
reconstructible.

## 1. Vérifier la synchronisation automatique

```bash
jq '{status,reason,as_of,total_notes,
     embeddings_backfilled,embeddings_pending,embedding_error,
     outcomes,more_work}' state/learnings_sync_status.json
```

État sain : `status="ok"`, `embeddings_pending=0` et
`embedding_error=null`. `outcomes=null` signifie simplement que l'évaluation
différée n'était pas due sur ce tick. Sa cadence normale est horaire ; si un lot
indique encore du travail, le worker peut enchaîner immédiatement le lot suivant.
Un fichier absent signifie qu'aucune synchronisation n'a encore abouti depuis
la création de cet état.

Si `embeddings_pending` reste positif :

- `embedding_error` renseigné indique l'échec du dernier lot ;
- `embedding_error=null` avec une clé absente signifie qu'aucun appel
  d'embedding n'a été tenté ;
- après modification de `OPENAI_API_KEY` dans `.env`, redémarrer le daemon pour
  que le worker de fond voie la nouvelle valeur, ou lancer immédiatement
  l'ingestion manuelle ci-dessous.

Les logs synthétiques du worker sont visibles avec :

```bash
rg '\[learnings_sync\]' state/daemon_console.log | tail -20
```

## 2. Reconstruire ou compléter le store dérivé

Avec embeddings :

```bash
uv run python scripts/learnings_ingest.py
```

Sans clé ou pour isoler ingestion et scoring :

```bash
uv run python scripts/learnings_ingest.py --no-embeddings
```

Le bilan JSON distingue les quatre sources, les verdicts appliqués, les scores,
les embeddings ajoutés et `total_notes`. La commande est idempotente : les
lignes déjà ingérées sont comptées comme `skipped`, pas dupliquées.

## 3. Rattraper les rationales LLM historiques

Toujours commencer en dry-run. `--since` est une borne ISO-8601 **exclusive** :

```bash
uv run python scripts/backfill_rationale_learnings.py \
  --since 2026-07-10T00:00:00+00:00
```

Contrôler `candidates`, `written=0` et les compteurs d'exclusion. Puis écrire
les seules expériences éligibles et absentes :

```bash
uv run python scripts/backfill_rationale_learnings.py \
  --since 2026-07-10T00:00:00+00:00 --apply
uv run python scripts/learnings_ingest.py
```

Le rattrapage écrit dans
`state/archive/rationale-experiences.jsonl`. Il reprend le même filtre que le
runtime : vraie décision LLM, rationale exploitable, aucun fallback synthétique,
puis déduplication par `decision_id`.

## 4. Recalculer les verdicts FLAIR historiques

Cette opération charge les prix 1 h et peut donc être longue ou partielle si
des symboles ne sont plus disponibles :

```bash
uv run python scripts/learnings_outcome_bootstrap.py
uv run python scripts/learnings_ingest.py
```

La première commande réécrit
`state/archive/learnings-outcome-bootstrap.json`; la seconde applique les
verdicts au store dérivé et recalcule les scores. Pour l'exploitation courante,
le daemon évalue déjà progressivement les outcomes matures : ne relancer ce
bootstrap complet que pour un rattrapage ou une reconstruction explicite.

## 5. Contrôler la consolidation en règles globales

L'inspection suivante est lecture-seule :

```bash
uv run python -m trader.agent.learnings.consolidator | jq .
```

Elle expose `raw_count`, `new_raw_count`, le watermark consolidé et la dernière
erreur éventuelle. La consolidation normale se fait à la fin d'un cycle du
daemon lorsqu'elle est due : 50 nouvelles notes, 10 feedbacks modifiés, ou un
rattrapage quotidien d'un changement en attente.

Contrôler les écritures réellement publiées :

```bash
jq -c 'select(.event == "learning_consolidated") |
  {ts,new_raw_count,written,curation_due,curated_candidate_count,error_code}' \
  state/events.jsonl | tail -10
```

`new_raw_count=0` n'est pas un run vide si `curation_due` vaut
`feedback_threshold` ou `daily_catch_up` : le consolidateur recure alors des
notes existantes dont le résultat a changé. `state/learnings_consolidated.json`
ne doit être considéré à jour qu'après `written=true`.

Éviter `python -m trader.agent.learnings.consolidator --run` en maintenance
normale : le chemin du daemon lui fournit en plus le pool SQLite enrichi par
FLAIR/MemRL et marque les révisions comme curées seulement après l'écriture
durable.

## Voir aussi

- [Référence Learnings & RAG](../reference/learnings-rag.md)
- [Lire les logs](read-logs.md)
