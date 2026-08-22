# Trace Brain des décisions Trader

> **Type** : Explanation (Diataxis). Contrat d'écriture additif, rétro-compatible.

Le premier enrichissement persisté relie un épisode Trader à ses identités
durables **sans reconstruire après coup**. Les lecteurs historiques ignorent
les clés nouvelles. Une identité manquante ou ambiguë reste `null` / `unavailable` :
rien n'est inventé depuis l'état courant.

## Identité d'épisode

`brain_trace.episode_id` n'existe que pour une décision Trader qui porte à la
fois un `task_id` de file decide et un `process_instance_id` uniques. Un HOLD
`decision_source=infra` n'est pas un épisode Brain, même si un processus a été
admis. `attempt_id`, `decision_id` et `mandate_id` sont propagés lorsqu'ils
sont disponibles, y compris sur les notes FLAIR ultérieures.

## Observation et post-effet

La timestamp pré-décision est le `bar_as_of` des barres d'analyse réellement
fournies. Si elle n'est pas capturée, le bloc `observation` déclare
`status=unavailable`. Le snapshot post-effet n'est pas encore produit : le
bloc `post_effect_snapshot` déclare la même absence au lieu d'un successeur
approximatif.

## FLAIR Trader

Chaque verdict persisté porte la base exacte (`realized` ou `counterfactual`),
l'horizon réellement utilisé (`1d` puis fallback `4h`), `evaluated_at` et
l'identité du cycle broker source. Deux cycles distincts pour la même décision
sont rejetés plutôt que départagés.

## Univers

La requête canonique, incluant le `selection_feedback` point-in-time, est
persistée dans `request_snapshot`. `prompt_observation` conserve séparément la
projection JSON structurée du prompt standard, sa version et son hash. Pour un
agent injecté qui ne passe pas par le tool loop standard, son statut dit
explicitement que cette projection n'est qu'une référence et que son prompt
réel est inconnu.

La tournée d'outils de l'agent Univers n'est plus jetée : `tool_trace` est
écrit sur le run avec `agent_run_id`, y compris lorsque aucun outil n'a été
appelé (`tool_rounds=0`). Il conserve les appels et les résultats exacts
réinjectés au modèle, pas seulement un compteur.

## Frontière world model

Cette trace ne prétend pas être un `WorldEpisode`. Une prochaine tâche due ne
constitue ni un snapshot post-effet ni un horizon de marché. La dynamique de
prix ne doit jamais être conditionnée causalement par l'action du paper trader.
Les rewards de sélection Univers, FLAIR Trader et MemRL restent des juges
séparés.
