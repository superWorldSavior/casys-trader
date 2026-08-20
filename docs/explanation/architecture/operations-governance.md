# Architecture — opérations et gouvernance

> **Type** : Explanation (Diataxis). Retour à l'[index Explanation](../README.md).

## LLM, transport et outils

Le transport LLM est une dépendance remplaçable : le protocole conserve un
contrat structuré, la provenance fournisseur/modèle et les erreurs. Une session
peut faire une tournée d'outils read-only bornée ; le compilateur du langage de
stratégie transforme ensuite la réponse en artefacts validables. Un outil ne
soumet jamais directement un ordre.

La queue durable isole les décisions longues et applique les limites de
ressources. Les détails de transport et d'isolation sont dans
[contrat LLM](../../reference/llm-contract.md),
[outils agent](../../reference/agent-tools.md) et
[file de tâches](../../reference/task-queue.md).

## Logs, preuves et pilote de processus

Les logs racontent l'exécution ; les ledgers et `process_events` permettent de
la prouver. Chaque instance de processus a une identité stable et des
tentatives/effets immuables. Le `ProcessPilot` admet et observe les symboles,
mais n'a aucune autorité pour changer un gate, sélectionner un modèle ou
envoyer un ordre.

Cette séparation permet une lecture cockpit et reporting sans transformer
l'observabilité en commande. Les runbooks opérateur restent dans
[How-to](../../how-to/README.md) ; les invariants de preuve sont dans
[process governance](../../reference/process-governance.md).
