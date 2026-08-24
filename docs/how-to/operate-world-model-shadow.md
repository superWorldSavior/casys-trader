# How-to — Opérer le World Model shadow

> **Type** : How-to (Diátaxis). Procédure opérateur.
> **Référence** : [`reference/world-model.md`](../reference/world-model.md)
>   (contrat V1 ; le pilote cohorte/macro/graphe est décrit ici et dans les
>   pages explanation / note de statut).
> **Pourquoi** : [`world-model-shadow.md`](../explanation/architecture/world-model-shadow.md),
>   [`world-context-ontology.md`](../explanation/architecture/world-context-ontology.md).
> **Statut pilote** : [`2026-08-24-world-model-shadow-pilot.md`](../decisions/2026-08-24-world-model-shadow-pilot.md).
>
> Cette page **ne redémarre pas**, ne pousse pas, et n'écrit pas dans `state/`
> ni `.env`. Un arrêt/relance reste un acte volontaire, voir
> [`run-the-daemon.md`](run-the-daemon.md).

Tout le World Model reste `shadow_only` / `decision_effect=none` / `NO_GO`.
Il n'influence ni le Brain, ni l'Univers, ni le scheduler, ni RiskGate, ni le
broker. Une métrique de marché n'est **pas** un PnL Trader. Après une semaine
de collecte supervisée : couverture, plomberie, tendances préliminaires —
jamais une preuve causale.

## Lire le statut

Depuis la racine du dépôt, sur l'état paper courant. Les lectures **ne créent
pas** `world_model.db` ni `state/world_macro/` :

```bash
uv run casys-trader world status --json
uv run casys-trader world macro status --json
uv run casys-trader world graph status --json
```

`world status` (`schema_version=world_model_status.v1`) porte aussi le bloc
macro. Champs à lire en premier :

| Champ | Lecture honnête |
|---|---|
| `status` | `not_started` si `state/world_model.db` n'existe pas encore |
| `authority` | toujours `shadow_only` |
| `decision_effect` | toujours `none` |
| `recommendation` | toujours `NO_GO` |
| `causal_claim` / `pnl_claim` | toujours `false` |
| `evaluation.status` | `warming_up` tant qu'aucune paire n'est scorable |
| `impact.actual_contribution.status` | `not_attributable` |
| `macro.gaps.gdelt` / `macro.gaps.news_macro_brief` | `excluded` |

`world graph status` (`world_graph_status.v1`) expose les budgets (profondeur
4, 32 chemins) et `gaps.writes`. Un graphe **câblé** n'est pas une écriture.

`world status` porte aussi `resource_budget` : décision live du garde-fou
disque (taille logique / on-disk de `world_model.db` et octets libres). Cette
lecture **ne crée pas** la base. `status=skipped` veut dire que le prochain
batch d'écriture shadow serait sauté, pas que le Trader s'est arrêté.

### Cohortes (status + rapport)

Le CLI n'a pas de `list`. Les `cohort_id` stables sont dérivés du YAML
(`pilot_id` + `schema_version` + `cohort_key` + `activation_policy`) et
apparaissent dans le log `[world_shadow_pilot]` au boot. Deux clés :

| Clé YAML | `study_kind` | Lanes |
|---|---|---|
| `technical_c1` | `pipeline_pilot` | Markov + GRU × `market` / `status_only` / `company` / `macro` / `joint` — **pas de graphe** |
| `graph_v3` | `pipeline_pilot` | Markov + GRU × `graph` seulement |

```bash
uv run casys-trader world cohort status --json COHORT_ID
uv run casys-trader world cohort report --json COHORT_ID
uv run casys-trader world graph report --json COHORT_ID
```

`world cohort report` (`world_cohort_report.v1`) est reconstructible et
lecture seule. `interpretation_limit=coverage_plumbing_preliminary_trends_only`
tant que l'empan de collecte est inférieur à 8 jours. `causal_claim` et
`pnl_claim` restent `false` même après.

## Distinguer boot, cycle dû, cycle idle

Trois états distincts. Ne pas les fusionner :

| Observation | Ce que ça prouve | Ce que ça ne prouve pas |
|---|---|---|
| Log `[world_model_shadow] enabled` / `[world_shadow_pilot] status=started` | câblage au boot, cohortes `register`/`arm`/`start` | qu'un épisode a été écrit |
| `world graph status` avec overlay câblé | le worker V3 est instancié | une projection persistée ou un `CAUSES` |
| `episodes_appended=0` au boot | l'activation **ne backfill pas** | un échec de plomberie |
| Cycle daemon `idle_waiting_for_wake` / replay de la même barre | le daemon vit | un nouveau `WorldEpisode` |
| Compteur d'épisodes qui monte | un **cycle dû** a capturé une ancre OHLCV valide | un effet Trader |
| `resource_budget.status=skipped` / log `stage=resource_budget` | last-resort disque : capture/training shadow sautés | un HOLD Trader, un arrêt daemon, une purge |

Le hook de capture tourne après le snapshot marché, **avant** le dispatch
LLM (`reason=market_snapshot_pre_dispatch`). Sans ancre OHLCV valide : **aucun
épisode**. Un replay exact est un no-op. Un cycle idle, un symbole unmapped
pour le graphe, ou un worker encore en file (`queued_latest`) peuvent donc
produire **zéro** nouvelle ligne alors que le daemon et le shadow sont
câblés. Le graphe le dit explicitement : `gaps.writes=none_until_due_cycle`.

Vérifier ensuite, dans cet ordre :

1. flags **du process déjà lancé** (un export local ne change rien) ;
2. `state/world_model.db` et, si le worker macro est on, `state/world_macro/` ;
3. logs `[world_model_shadow]`, `[world_shadow_pilot]`,
   `[world_macro_source_only]` dans `state/daemon_console.log`.

## Fenêtre d'une semaine

La fenêtre n'est **pas** une date ISO pré-expirée dans le YAML. Le calendrier
supervisé commence le **2026-08-24, Asia/Taipei**. Le runtime estampille :

- `planned_start_not_before` = horloge UTC du **premier** boot qui active
  (`window.planned_start=boot_event_time`) ;
- `collection_stop_at` = start + 7 jours (`fixed_end`).

Un second boot est idempotent : il **ne décale pas** la fenêtre, ne
ré-écrit pas d'épisode, et répare au plus les reçus d'availability. Relire
`world cohort status` : `phase=collecting` et les deux timestamps gelés.

## Mapping / ontologie v2 (rollout, pas de backfill)

Le hot-set live n'est plus couvert par les 8 ancres exactes de
`world_scope_mapping.v1`. Le YAML committe `world_scope_mapping.v2` +
`market_ontology.v2` (table exacte `(market_venue, instrument)`, preuves
suffixe/`sessions` existantes, **aucun** fallback runtime).
`lifecycle_generation` du pilote passe à **5** : nouvelles identités de
cohorte, le contrat producteur/lane/plan de collecte `macro_source_only.v2`
est gelé dans le YAML. Les identités des générations 3/4 ne sont pas
réutilisées ; les cohortes déjà terminales ne sont pas ranimées.

Après déploiement du commit, **un redémarrage volontaire** du daemon :

1. le boot publie `market_ontology.v2` ; si `world_model.db` a déjà
   `market_ontology.v1`, il la **supersède** (append-only), il ne mute pas
   le hash `v1` ;
2. le pont macro→graphe, s'il était `blocked=config_drift`, fait un
   **handoff** déterministe vers le spec `v2` (curseur conservé, pas de
   double ownership, pas de backfill) ;
3. `world graph status` / `world cohort status` doivent montrer les têtes
   `world_scope_mapping.v2` / `market_ontology.v2` ;
4. un symbole du hot-set sans ligne exacte reste `unmapped` (missingness),
   jamais une invention MIC.

Pas de `DELETE`/`VACUUM` du ledger. `shadow_only` / `decision_effect=none`
/ `causal_claim=false` / `pnl_claim=false` inchangés. Si le YAML mapping
change à mapping_id constant, le boot **conflit** — bump d'id obligatoire.

## Claims autorisés après une semaine

Après sept jours de collecte supervisée, on peut dire :

- la couverture (slots, capteurs, venues, horizons) ;
- que la plomberie capture / masks / appariement / restart tient ou casse ;
- des **tendances préliminaires** descriptives (log-loss / Brier appariés),
  sous `insufficient_support` tant que le minimum descriptif (20 ancres
  uniques) n'est pas atteint.

On ne peut **pas** dire :

- qu'une voie *cause* un mouvement (`causal_claim=false`, pas d'arête `CAUSES`) ;
- qu'un PnL Trader, un drawdown de portefeuille ou un uplift d'ordres
  existent (`pnl_claim=false`, `actual_trader_contribution=not_attributable`) ;
- qu'une étude `prospective_evaluation` a été close — les deux cohortes du
  pilote sont `pipeline_pilot`.

## Flags et YAML

Tous les flags sont lus **uniquement au boot**. Défauts runtime :

| Flag | Défaut | Effet |
|---|---|---|
| `CASYS_WORLD_MODEL_SHADOW_ENABLED` | `1` | shadow V1 (Markov + GRU marché) |
| `CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED` | `0` | voies contexte V2 |
| `CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED` | `0` | worker macro source-only |
| `CASYS_WORLD_MODEL_GRAPH_V3_ENABLED` | `0` | voies graphe V3 |
| `CASYS_WORLD_SHADOW_PILOT_ACTIVATION` | `1` | honore le YAML d'autorisation |

Le YAML `config/world_shadow_pilot.yaml` est l'**autorisation opérateur**, pas
un défaut RFC. Si `CASYS_WORLD_SHADOW_PILOT_ACTIVATION=1` **et**
`enabled: true`, le boot fait un **OU** avec `workers.*` :

- `workers.v1_shadow` — V1 déjà porté par `CASYS_WORLD_MODEL_SHADOW_ENABLED` ;
- `workers.context_v2` → OR du flag V2 ;
- `workers.macro_source_only` → OR du flag macro ;
- `workers.graph_v3` → OR du flag graphe.

Puis, si le store V1 est créé, `activate_world_shadow_pilot` enregistre, arme
et démarre les deux cohortes (exception documentée à l'activation RFC).
`=0` saute register/arm/start **et** le OU YAML ; le V1 reste si son flag
est on.

`config/world_graph_v3.yaml` garde `cohort_id: null`. L'id graphe est
**injecté** au compose depuis le rapport d'activation, jamais lu dans ce
fichier.

## Garde-fou budget disque (last-resort)

Le shadow a un **plafond de dernier recours**, local au World Model. Il ne
remplace pas le dédoublonnage sémantique. Il ne touche ni Trader, ni Brain,
ni Univers, ni broker, ni `casys.db`.

Fichier versionné : `config/world_shadow_resource_budget.yaml`
(`schema_version=world_shadow_resource_budget.v1`, `content_sha256` canonique).
Pas de lecture magique d'environnement pour les seuils. YAML absent ou
invalide → les défauts conservateurs restent actifs.

Défauts du pilote d'une semaine (machine ~7 GiB libres, capture polluée
~2 MiB/min) :

| Seuil | Défaut | Effet |
|---|---|---|
| `max_db_bytes` | 2147483648 (2 GiB) | saute le batch si la taille logique **ou** on-disk (`world_model.db` + WAL/SHM) atteint le plafond |
| `min_free_bytes` | 3221225472 (3 GiB) | saute le batch si le filesystem a moins que cette réserve |
| `warn_interval_seconds` | 300 | warning structuré `[world_model_shadow]` au plus une fois par intervalle |

Avant **chaque** batch d'écriture (un `stat` par cycle, pas par épisode) :
si le plafond DB ou la réserve libre est franchi, le worker **saute seulement**
capture + entraînement World Model de ce cycle. Le daemon continue. Le chemin
de décision Trader n'est pas appelé par ce garde-fou.

Le garde-fou **ne fait jamais** : `DELETE` / `TRUNCATE` / `VACUUM`, arrêt du
daemon, mutation de l'historique append-only. Relire `world status` ne crée
pas `world_model.db`.

Raisons : `within_budget`, `db_size_exceeded`, `free_space_below_reserve`,
`probe_error` (échec du port filesystem → skip fail-safe des écritures
shadow, Trader inchangé).

Pour desserrer le plafond : éditer le YAML (rehash `content_sha256`), puis
redémarrage volontaire. Ne pas baisser `min_free_bytes` sous ce qu'il faut
aux logs / `casys.db` / OS.

## Arrêter / désactiver le pilote

Sans toucher au process vivant. Choisir l'intention, puis un arrêt/relance
volontaire.

| Objectif | Action | Ce qui reste |
|---|---|---|
| Ne plus auto-activer au prochain boot | `CASYS_WORLD_SHADOW_PILOT_ACTIVATION=0` | V1 si `SHADOW_ENABLED=1` ; les cohortes **déjà** `collecting` dans `world_model.db` restent collectantes jusqu'à `close` / `invalidate` |
| Couper V2 / macro / graphe au prochain boot | flags concernés à `0` **et** soit activation `0`, soit `workers.*: false` (rehash `content_sha256`) | V1 |
| Fermer la collecte d'une cohorte | `uv run casys-trader world cohort close COHORT_ID --reason TEXT --json` | ledger append-only intact |
| Invalider le protocole | `uv run casys-trader world cohort invalidate COHORT_ID --reason REASON --json` | `complete` est terminal : on n'invalide pas après |
| Couper tout le shadow | `CASYS_WORLD_MODEL_SHADOW_ENABLED=0` | `world_model.db` n'est pas effacé ; **pas** de register/arm/start (pas de store). Le worker macro peut encore tourner si le YAML/flag l'OR : le couper aussi (ligne workers ci-dessus). Le chemin de décision Trader ne change pas |

Raisons d'`invalidate` (enum fermé) : `manifest_id_hash_conflict`,
`accepted_drift`, `pre_start_episode`, `contaminated_macro`, `future_leak`,
`unpaired_training`, `missing_lane_metadata`, `destructive_correction`.

`register` / `arm` / `start` CLI restent disponibles : le défaut RFC est
l'activation **explicite**. Le YAML `operator_authorized_on_boot` est
l'exception humaine, pas un feu vert trading.

## Activer / couper le V1 seul

| Objectif | Action |
|---|---|
| Shadow V1 on (défaut) | omettre le flag, ou `CASYS_WORLD_MODEL_SHADOW_ENABLED=1`, **puis redémarrer** |
| Shadow V1 off | `CASYS_WORLD_MODEL_SHADOW_ENABLED=0`, **puis redémarrer** |

Le chemin de décision Trader ne change pas. Couper le shadow n'efface pas
`world_model.db`.

## Lire l'évaluation sans sur-interpréter

Quand `evaluation.status=ready` (voie V1) :

- comparer baseline et GRU seulement si `comparisons[].status=ready` ;
- en dessous de 20 paires : `insufficient_support`, pas de vainqueur ;
- le drawdown directionnel est un proxy de marché à notionnel 1, sans frais ;
  ce n'est pas le drawdown du portefeuille paper.

Quand `world cohort report` :

- `study_kind=pipeline_pilot` : technique uniquement ;
- `support` / `gates.support` : mode `descriptive_only` sur ce pilote ;
- `negative_controls.status=offline_only` : les permutations ne trainent
  pas les voies live ;
- `winner` reste `false` dans les claims bornés du rapport.

Quand `impact.actual_contribution.status=not_attributable` : ne pas
attribuer de PnL Trader au World Model.

## Isoler la preuve

| Store | Contenu | Autorité |
|---|---|---|
| `state/world_model.db` | épisodes, outcomes, prédictions, événements de cohorte | journal append-only du shadow |
| `state/world_macro/` | faits / observations / runs source-only + reçus | producteur macro, pas Univers |
| `state/gdelt/` et `state/news_briefs/` | GDELT et `NewsMacroBrief` **Univers** | **exclus** comme source World Context |
| `casys.db` | broker, décisions Trader | jamais fusionné |

Ne pas copier de tables broker dans `world_model.db`, ni y ouvrir
d'écriture manuelle. Le query adapter et le CLI status/report sont en
lecture seule ; le store runtime refuse `UPDATE`/`DELETE`. NetworkX n'est
jamais persisté.
