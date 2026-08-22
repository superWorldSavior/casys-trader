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
[world model shadow](world-model-shadow.md) utilise désormais un corpus
prospectif séparé ; cet export historique n'en est toujours pas une source
d'entraînement.

## Frontière du `WorldEpisode` prospectif

Un vrai dataset de dynamique devra capturer, de manière append-only :

1. l'observation exacte avant décision, y compris les mémoires point-in-time
   présentées à Univers et au Brain ;
2. un outcome exogène du marché ou de la situation à horizon fixe, sans action
   Trader dans la transition de prix ;
3. le mandat Univers et la décision Trader comme contrôles/hypothèses séparés ;
4. des critics séparés pour la sélection Univers et la décision Trader ;
5. la provenance `available_at`, la fraîcheur et les états `missing/stale/unknown`.

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
