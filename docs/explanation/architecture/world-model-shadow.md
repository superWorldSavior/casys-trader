# World model de marché — expérimentation shadow

> **Type** : Explanation (Diataxis). Cette capacité est observationnelle et
> `shadow_only` : elle ne conseille pas le Brain et ne pilote ni scheduler,
> RiskGate, broker, portefeuille, mandat Univers, ni mémoire.

## Question à laquelle il répond

Le world model estime une transition exogène :

> « À partir d'un état de marché réellement disponible à T0, quelle est la
> probabilité que ce marché soit `DOWN`, `FLAT` ou `UP` à un horizon fixe ? »

Il ne répond pas directement à « ce trade était-il bon ? ». Cette seconde
question reste celle du critic Trader/FLAIR, qui peut comparer l'action choisie
à la trajectoire observée. D16 juge la sélection Univers et MemRL juge l'utilité
des rappels. Ces trois critics ne sont jamais fusionnés avec la cible marché.

## Flux V1

```text
snapshot marché frais, avant les gates et le LLM
  -> WorldEpisode immuable pour chaque symbole tradable
  -> prédictions 4 h et 1 j persistées avant leurs labels
  -> outcomes marché indépendants à T+4 h et T+1 j
  -> mise à jour incrémentale du baseline
  -> mesure préquentielle des prédictions shadow
```

Le sampling porte sur tout `tradable_symbols`, pas sur les seuls symboles dus,
admis par le scheduler ou envoyés au modèle. L'identité canonique combine la
place, le symbole, l'intervalle, la barre T0 et les versions de contrats de
features et de sampling. Un replay strict est un no-op ; une même identité avec
un contenu de marché différent est un conflit, jamais un overwrite. Deux wakes
sur la même barre réutilisent en revanche la première preuve canonique : une
heure de fetch ou un âge de fraîcheur plus tardifs ne créent pas un faux nouvel
état.

La V1 utilise les snapshots déjà chargés par les cycles Trader. Elle retire donc
le biais de sélection *entre symboles* dans un cycle, mais ne prétend pas encore
constituer une horloge de marché continue indépendante de la cadence des cycles.
La politique de sampling le dit explicitement afin qu'une politique périodique
future ne soit pas mélangée au même cohort.

## Observation et labels

L'observation est construite par whitelist à partir des seules barres et
métadonnées de marché disponibles à T0. Ce cutoff commun est pris après la fin
des I/O du snapshot, jamais à l'heure antérieure de début du cycle. Elle conserve notamment source,
intervalle, sémantique du timestamp, fraîcheur, `available_at`, barre d'ancrage,
features causales et hashes de preuve.

Elle exclut action, intent, quantité, confiance, prompt, outils, tâche,
scheduler, portefeuille, risque, fills et PnL. Les mandats/hypothèses Univers et
la décision Brain restent des contrôles/provenances séparés dans leurs traces
canoniques ; ils peuvent être joints pour une analyse de critic, mais le feature
builder du world model ne les lit pas.

Les horizons `elapsed_4h.v1` et `elapsed_1d.v1` sont calculés séparément depuis
la barre T0. Chaque label utilise la première barre admissible à ou après sa
cible, conserve sa preuve et son retard réel, et peut rester
`pending`, ou se fermer en `observed`, `missing` ou `unknown`. Seul `pending`
est recalculé ; les états terminaux sont persistés pour éviter une rematuration
sans borne. Il n'existe aucun fallback silencieux de 1 j vers 4 h. Le store
accepte une correction future comme outcome supersédant ; un
producer de correction devra l'ajouter explicitement et ne modifiera jamais
une preuve existante.

## Modèle V1 et futur GRU

Le premier challenger est un Markov catégoriel hiérarchique à lissage
Dirichlet : état exact, puis état grossier, puis fréquence globale, puis
distribution uniforme au cold start. Il est déterministe, incrémental et rend
visibles son support et son niveau de backoff. Tant que le support est
insuffisant il émet `warming_up/NO_GO`; avec davantage de données il reste
malgré tout `shadow_only`.

Un GRU éventuel devra consommer le même contrat d'observation causal et produire
le même contrat de prédiction. Il sera donc un challenger remplaçable, pas une
nouvelle source d'autorité. On ne l'entraînera qu'après avoir accumulé assez de
séquences prospectives point-in-time pour comparer son Brier score, sa
calibration et sa stabilité au baseline simple.

## Persistance et sûreté

`state/world_model.db` est le journal canonique dédié de cette preuve live. Il
n'est ni une table de `casys.db`, ni une projection de `decisions.jsonl`.
Episodes, outcomes et prédictions sont append-only ; SQLite WAL et des
transactions courtes assurent l'idempotence, et les `UPDATE/DELETE` sont
refusés.

Le hook runtime est fail-open et isolé par symbole/horizon. Toute panne du
store, du labeler ou du modèle est rapportée dans le résultat shadow, sans
modifier ni faire échouer le cycle métier. Les décisions historiques n'ayant
pas de snapshot T0 exact peuvent servir à l'audit de jointure, mais restent
`training_eligible=false` et ne sont pas injectées dans ce modèle.

Le shadow est activé par défaut au prochain démarrage du daemon. La variable
`CASYS_WORLD_MODEL_SHADOW_ENABLED=0` permet de le désactiver sans toucher au
chemin de décision.

Le statut est une lecture seule : il ne crée ni ne migre la base absente.

```bash
uv run casys-trader world status --json
```

Il expose la couverture, les horizons observés et, dès que des prédictions ont
mûri, les métriques préquentielles (Brier, skill contre l'uniforme, log-loss,
accuracy et calibration). Une base vide répond `warming_up`.
