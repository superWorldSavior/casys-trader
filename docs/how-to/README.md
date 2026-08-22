# How-to opérateur

> **Type** : How-to (Diátaxis).
> Ces pages sont des procédures. Elles doivent pointer vers les références
> canoniques au lieu de recopier toute la logique runtime.

| Besoin | Page | Vérifier aussi |
|---|---|---|
| Lancer, relancer ou arrêter le daemon | [`run-the-daemon.md`](run-the-daemon.md) | [`reference/wake-scheduler`](../reference/wake-scheduler.md), [`reference/task-queue`](../reference/task-queue.md) |
| Lire les logs et les events | [`read-logs.md`](read-logs.md) | [`reference/reporting`](../reference/reporting.md), [`reference/wake-scheduler`](../reference/wake-scheduler.md) |
| Changer et vérifier le preset des cinq rôles LLM | [`manage-model-presets.md`](manage-model-presets.md) | [`reference/model-presets`](../reference/model-presets.md), [`reference/codex-home`](../reference/codex-home-isole.md) |
| Maintenir le store de learnings, les embeddings et les règles | [`maintain-learnings.md`](maintain-learnings.md) | [`reference/learnings-rag`](../reference/learnings-rag.md) |
| Rafraîchir et diagnostiquer les rapports global/macro/régional/micro | [`refresh-and-diagnose-reports.md`](refresh-and-diagnose-reports.md) | [`reference/news`](../reference/news.md), [`reference/universe`](../reference/universe-rotation.md), [`reference/company`](../reference/company-intelligence.md) |
| Mesurer l'activité ou rejouer des plans | [`measure-and-replay.md`](measure-and-replay.md) | [`reference/reporting`](../reference/reporting.md), [`reference/execution`](../reference/execution.md) |
| Lire le World Model shadow sans l'attribuer au Trader | [`operate-world-model-shadow.md`](operate-world-model-shadow.md) | [`reference/world-model`](../reference/world-model.md) |
| Configurer l'apparence cockpit dans le terminal | [`cockpit-glass.md`](cockpit-glass.md) | [`reference/cockpit`](../reference/cockpit.md) |

Avant de prendre une décision opérationnelle depuis un how-to, vérifier l'état
live (`state/daemon_status.json`, `state/current_report.json`, process daemon).
