# État du système — casys-trader

> **Type** : instantané vivant. Cette page dit ce qui a été vérifié sur le
> runtime et résume le socle actuel ; les contrats détaillés restent dans les
> pages de référence.

**Dernière vérification : 2026-08-11, 11:55 Asia/Taipei (03:55 UTC).**

## État observé

| Surface | Observation | Portée de la preuve |
|---|---|---|
| Daemon paper | Le processus supervisé `python -m trader.daemon --live` est vivant. `state/daemon_status.json` indiquait `cycle_started`, sans symbole dû ni appel modèle dans ce cycle à l'instant de lecture. | Échantillon ponctuel : la phase change au fil du cycle. Voir [lancer le daemon](how-to/run-the-daemon.md). |
| Code servi | Le dernier rapport runtime identifie la branche `main`, commit `f95c25dbd1ec`, avec un worktree non propre. | Le commit est la base chargée ; les modifications locales en cours ne sont pas attestées comme servies. |
| Modèles demandés | `make models` retourne le preset `codex-luna-medium` : brain `gpt-5.6-luna` avec effort `medium`, consolidateur, univers, micro et macro en `gpt-5.6-sol` avec leur profil commun `low`. | C'est la configuration demandée par l'environnement. Voir [presets de modèles](reference/model-presets.md). |
| Modèle réellement observé | Une décision durable du 2026-08-11 à 03:40 UTC porte `model_called=true`, `decision_source=llm`, provider `acpx`, modèle `gpt-5.6-luna`, sans fallback. | Cela prouve le brain pour cet appel, pas le modèle servi par tous les rôles ni les appels futurs. |
| Gouvernance du cycle | Les dernières décisions portent `process_instance_id`, `attempt_id`, `runtime_run_id`, `decision_id` et l'empreinte du bundle de gouvernance. | Le pilote est intégré à ce run et produit la corrélation attendue. Voir [gouvernance du processus](reference/process-governance.md). |
| Learnings | `state/learnings_sync_status.json` indiquait `status=ok`, aucune vectorisation en attente et aucune erreur d'embedding. | État du dernier rattrapage `post_cycle`, pas une promesse sur le prochain fournisseur d'embeddings. Voir [maintenir les learnings](how-to/maintain-learnings.md). |
| Briefs macro/news | Les quatre portées `GLOBAL`, `EU`, `US` et `TW` avaient chacune un dernier succès et aucune `latest_failure` au moment du contrôle. | Santé ponctuelle du runner macro/news uniquement. Voir [rafraîchir les rapports](how-to/refresh-and-diagnose-reports.md). |
| Rapports Univers régionaux | EU et US conservaient un dernier succès sans panne récente. TW conservait son dernier succès ainsi qu'une tentative ultérieure `nonzero_exit` dans `latest_failure`. | Le dernier succès reste affichable pendant le retry ; succès projeté et dernière tentative sont deux faits distincts. |

## Socle actuel

### Cycle, état et exécution

- Le daemon est l'unique orchestrateur live paper. Le supervisor protège le
  PID et refuse les doublons.
- `state/casys.db` porte l'état SQLite canonique. Les décisions et ordres
  passent par les files durables `decide` et `execute`, actives dans
  l'environnement observé ; les JSON/JSONL servent de ledgers ou de
  projections selon le domaine.
- Le scheduler, les plans, le `RiskGate` et le broker paper restent
  déterministes autour de la décision LLM. Les retries de file sont bornés et
  passent en `dead` lorsque leur budget est épuisé.
- Le pilote de processus est observationnel : il ajoute identités, version de
  gouvernance et preuves de readback. Il ne retire aucun symbole dû et ne
  décide pas si le LLM doit être appelé.

Références : [architecture](architecture.md), [file de tâches](reference/task-queue.md),
[exécution](reference/execution.md), [RiskGate](reference/risk-gate.md) et
[gouvernance du processus](reference/process-governance.md).

### Brain et outils

- Le brain reçoit un contexte compact puis peut appeler les outils de domaine
  autorisés pour obtenir des faits ou proposer des actions structurées. Le
  daemon garde la validation et l'exécution finales.
- Les outils métier sont enregistrés dans le runtime Python ; l'isolation du
  profil Codex ou l'absence d'outils natifs dans le transport ACP ne les
  supprime pas.
- `model_called=false` désigne une décision synthétique d'infrastructure ; une
  ligne `HOLD` ne suffit donc jamais à prouver un appel LLM.

Références : [outils de l'agent](reference/agent-tools.md),
[contrat LLM](reference/llm-contract.md), [contexte agent](reference/agent-context.md)
et [profil Codex isolé](reference/codex-home-isole.md).

### Learnings

- Les rationales des vraies décisions LLM sont capturées automatiquement ;
  `record_learning` reste une annotation volontaire, pas la seule source de
  mémoire.
- Le store dérivé ingère les rationales, notes runtime, notes évincées et
  historique consolidé. La recherche FTS est disponible immédiatement ; les
  embeddings sont rattrapés en arrière-plan.
- Les outcomes différés scorent les notes quand ils deviennent disponibles et
  la consolidation produit des règles réinjectables. Le tout reste
  reconstructible depuis les sources durables.

Référence : [learnings et RAG](reference/learnings-rag.md).

### Intelligence et univers

- Les analyses micro société alimentent les rapports régionaux ; un nouveau
  brief micro ne relance le régional que si la signature d'entrée projetée a
  réellement changé.
- Les briefs macro/news `GLOBAL`, `EU`, `US` et `TW`, les runs Univers régionaux
  et la posture globale conservent séparément dernier succès, dernier échec et
  lignée de retry. Les cadences, cooldowns et backoffs sont bornés et visibles
  dans les logs normaux.
- La rotation d'univers relie scope candidat, challengers news, brief régional,
  proposition LLM préparée puis activation. L'activation reste distincte du
  succès de génération.
- Le cockpit projette santé, logs, décisions, univers, configuration et galerie
  de rapports sans devenir une nouvelle source de vérité.

Références : [intelligence société](reference/company-intelligence.md),
[macro et news](reference/macro.md), [pipeline univers](reference/universe-trader-pipeline.md),
[rotation d'univers](reference/universe-rotation.md) et [cockpit](reference/cockpit.md).

## Limites connues

- `state/situation_memory.db` existe comme index de points de briefs, mais le
  runtime ne fait pas encore de retrieval de cette mémoire pour décider.
- Une instance de processus en `recovery_required` conserve ses références
  causales, mais la clôture après réconciliation n'est pas encore implémentée ;
  aucun succès terminal ne doit être inféré d'un effet inconnu.
- Un preset affiché par `make models` décrit la demande. Seule une trace
  durable `model_called=true` avec provider/modèle prouve ce qui a réellement
  servi un appel.
- Cette page vieillit par définition. Pour un diagnostic, relire les artefacts
  live plutôt que recopier les observations ci-dessus.

## Contrôle opérateur

```bash
uv run casys-trader status --json
make models
ps -p "$(tr -d '[:space:]' < state/daemon.pid)" -o pid=,etime=,stat=,command=
jq '{phase,ts,current_symbol,symbols_due,model_calls_used}' state/daemon_status.json
jq '{status,reason,as_of,embeddings_pending,embedding_error}' state/learnings_sync_status.json
jq 'to_entries | map({venue:.key,last_success_at:.value.last_success_at,latest_failure:.value.latest_failure})' state/news_macro_analysis_status.json
```

Pour l'exploitation quotidienne : [lire les logs](how-to/read-logs.md),
[lancer le daemon](how-to/run-the-daemon.md),
[gérer les modèles](how-to/manage-model-presets.md),
[maintenir les learnings](how-to/maintain-learnings.md) et
[diagnostiquer les rapports](how-to/refresh-and-diagnose-reports.md).
