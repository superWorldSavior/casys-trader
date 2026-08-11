# Tutoriel — Suivre une décision paper jusqu'à sa preuve

> **Type** : Tutorial (Diátaxis) — parcours guidé, lecture seule.
> **Pré-requis** : au moins un cycle paper terminé, `jq` et `sqlite3`.

À la fin de ce parcours, tu sauras distinguer ce que le modèle a réellement
dit de ce que l'infrastructure a synthétisé, puis relier une décision durable à
son instance de processus.

## 1. Choisir une décision corrélée

Depuis la racine du dépôt :

```bash
decision_id=$(jq -r \
  'select(.process.process_instance_id != null) | .decision_id' \
  state/decisions.jsonl | tail -1)
printf '%s\n' "$decision_id"
```

Si la sortie est vide, l'état ne contient pas encore de décision produite avec
le pilote intégré. Attendre un cycle paper ; ne fabriquer aucun ID à la main.

## 2. Lire la décision, sans confondre HOLD et appel modèle

```bash
jq -c --arg id "$decision_id" \
  'select(.decision_id == $id) |
   {cycle_ts,symbol,action,intent,model_called,decision_source,
    llm_provider,llm_model,llm_error,reason,process}' \
  state/decisions.jsonl
```

Observe surtout :

- `model_called=true` et `decision_source="llm"` prouvent une décision du brain ;
- `action="HOLD"` ne le prouve pas à lui seul ;
- `model_called=false` avec une source infra décrit une décision synthétique,
  par exemple un gate de fraîcheur ;
- `process` fournit les IDs de jointure, pas une nouvelle décision métier.

## 3. Extraire l'instance de processus

```bash
process_id=$(jq -r --arg id "$decision_id" \
  'select(.decision_id == $id) | .process.process_instance_id' \
  state/decisions.jsonl)
printf '%s\n' "$process_id"
```

Le `process_instance_id` reste stable pour cet objet de travail tant que
l'instance est ouverte. `attempt_id` change lors d'une reprise ;
`runtime_run_id` identifie le démarrage du daemon.

## 4. Lire le lifecycle append-only

```bash
sqlite3 -header -column state/casys.db \
  "SELECT seq,event_type,attempt_id,outcome_code,effect_status,terminal_result
   FROM process_events
   WHERE process_instance_id='$process_id'
   ORDER BY seq;"
```

Une histoire nominale contient `instance_admitted`, puis éventuellement des
tentatives intermédiaires, et enfin `instance_closed`. La colonne
`terminal_result` n'est renseignée que sur cette clôture. En son absence,
l'instance n'est pas terminée, même si une décision existe dans le JSONL.

## 5. Vérifier un fill éventuel

```bash
sqlite3 -header -column state/casys.db \
  "SELECT seq,symbol,side,quantity,price,ts,attempt_id,decision_id
   FROM broker_fills
   WHERE process_instance_id='$process_id'
   ORDER BY seq;"
```

Un HOLD ou un ordre rejeté rend normalement zéro ligne : l'absence de fill
n'est alors pas une panne. Pour une exécution paper, `decision_id` et
`attempt_id` doivent correspondre à la décision et à l'événement. Le runtime
fait cette même comparaison avant d'attester l'effet.

## 6. Ce que tu viens de démontrer

Tu as suivi trois vérités distinctes :

1. `decisions.jsonl` dit quelle décision a été durablement enregistrée et si le
   modèle a réellement été appelé ;
2. `process_events` dit où en est l'instance observée et si elle est close ;
3. `broker_fills` prouve un effet d'exécution, lorsqu'il y en a un.

Aucune de ces surfaces ne remplace les deux autres. Pour le contrat complet,
continuer avec [gouvernance du processus](../reference/process-governance.md),
[reporting](../reference/reporting.md) et
[exécution](../reference/execution.md).
