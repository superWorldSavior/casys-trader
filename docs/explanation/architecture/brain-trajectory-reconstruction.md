# Dataset observationnel Brain–Univers — audit offline

> **Type** : explication d'un outil de recherche en lecture seule. Ce fichier
> ne décrit ni une capacité runtime active, ni un world model.

`scripts/brain_trajectory_spike.py` rapproche les traces durables existantes
pour auditer le chemin suivant :

```text
scope candidat Univers
  -> run Univers et mandat observé
  -> tâche Trader et observation réellement présentée
  -> tentatives, outils et décision
  -> effets, fills et outcomes ultérieurs
```

L'export conserve séparément :

- la qualité de sélection Univers (`allocation` et `direction`, D16) ;
- le FLAIR Trader (`realized` ou `counterfactual`) ;
- l'utilité mémoire MemRL ;
- l'exécution et le PnL comme provenance économique.

Ces signaux ne forment jamais une récompense commune. `mandate_id` est une clé
de contexte et de provenance, pas une attribution causale du résultat Trader à
l'agent Univers.

## Ce que l'export permet

- mesurer la couverture et la qualité des jointures ;
- analyser les patterns de workflow, retries et outils ;
- comparer observationnellement un critic Trader avec et sans contexte Univers ;
- distinguer ce que le modèle a réellement vu de ce qui a été reconstruit.

`cross_loop_episodes.jsonl` est une vue observationnelle point-in-time. Le
champ brut `later_task_observation` n'existe que dans `trajectories.jsonl` pour
diagnostiquer la chronologie du scheduler. Il désigne la prochaine tâche due du
même symbole, à délai variable. Ce n'est jamais un état futur de marché ni une
transition `s -> s'` entraînable.

## Ce que l'export ne permet pas

- conclure que le mandat Univers cause une meilleure ou une moins bonne décision ;
- apprendre une dynamique de prix conditionnée par l'action du Trader ;
- appeler la prochaine tâche due un horizon de marché ;
- fusionner D16, FLAIR Trader et MemRL ;
- entraîner ou activer un GRU depuis cet export reconstruit.

Les anciens prototypes qui faisaient ces assimilations ont été retirés. Le
[world model shadow](world-model-shadow.md) utilise un corpus prospectif
séparé, déjà implémenté ; cet export historique n'en est **pas** une source
d'entraînement.

## `WorldEpisode` : marché action-free, déjà implémenté

`trader.domain.world_episode.WorldEpisode` existe. C'est une observation de
marché append-only (ancre OHLCV, fraîcheur, features causales, `available_at`).
Il ne contient **jamais** de mémoire Brain, de mémoire Univers, de mandat, de
décision, d'ordre, de fill ou de portefeuille. Sans ancre OHLCV valide, aucun
épisode n'est créé. Les labels `elapsed_4h.v1` / `elapsed_1d.v1` et les
prédictions shadow vivent dans `state/world_model.db`, pas dans
`cross_loop_episodes.jsonl`.

Ce spike reste un audit du chemin Univers → Brain → fills. Ce n'est pas le
dataset World Model et il ne doit pas y être fusionné. Les mémoires
point-in-time Brain/Univers restent de la provenance de décision, pas des
features de transition de marché.

Le portefeuille peut ensuite être simulé conditionnellement à une action et à
la trajectoire de prix. Ce simulateur n'est pas le modèle causal du marché.

## Exécution

Le mode par défaut ne crée aucun fichier :

```bash
uv run python scripts/brain_trajectory_spike.py --json
```

Un export exige un répertoire nouveau ou vide :

```bash
spike_dir=$(mktemp -d /tmp/casys-brain-trace.XXXXXX)
uv run python scripts/brain_trajectory_spike.py \
  --include-shared-context \
  --output-dir "$spike_dir" \
  --json
```

Les bases SQLite sont ouvertes en lecture seule et l'export refuse un WAL non
checkpointé. Une absence de donnée reste explicite ; elle n'est jamais comblée
depuis l'état courant.
