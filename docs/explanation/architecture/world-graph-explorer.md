# Explorateur World Graph — projection Desktop lecture seule

> **Type** : Explanation (Diataxis). Capacité **shadow_only** : elle ne conseille
> pas le Brain et n'a aucun effet de décision. Ontologie / PIT :
> [world-context-ontology](world-context-ontology.md).
> Contrat marché : [world-model-shadow](world-model-shadow.md).

## Question à laquelle il répond

Desktop a besoin de voir le graphe **canonique déjà publié**, pas un atlas
d'influence heuristique et pas une reconstruction à un cutoff historique
arbitraire.

La projection dit :

> « Quelle est la tête structurelle courante, et quelles relations de
> connaissance sont liées à **exactement** cette révision active ? »

Elle ne dit pas qu'une arête *cause* un mouvement de marché. Elle n'invente
jamais `CAUSES`.

## Contrat

```text
schema_version   = world_graph_explorer.v1
view             = current_published_overview
authority        = shadow_only
decision_effect  = none
causal_claim     = false
```

`generated_at` est l'heure de construction de la projection. `cutoff_at` est
l'horizon PIT utilisé pour cette vue courante. Ce sont deux champs distincts.
La révision active et ses hashes sont dans `ontology` / `revision`.
`truncated` répète le booléen de `truncation`. Cette voie ne revendique
**pas** un cutoff historique paramétrable.

GET `/api/world-graph` (ressource pont `world-graph`) est le seul transport
Desktop. Aucun query `cutoff`.

## Un seul graphe produit, deux sources de vérité complémentaires

L'interface ne juxtapose pas un « ancien graphe D3 » et un « nouveau graphe
NetworkX ». Elle construit **une seule projection produit** :

```text
World Graph publié (identité + géographie)
                  │
                  ├── lie chaque company visible à instrument → venue → pays
                  │
Intelligence courante (market + domain + family + driver)
                  │
                  └── projection D3 unique, navigation et look & feel existants
```

NetworkX reste un moteur de calcul shadow et n'est jamais sérialisé vers le
navigateur. `/api/world-graph` expose uniquement les objets et relations JSON
du ledger publié. L'adaptateur Desktop replie pour l'instant `symbol`,
`instrument` et `company` sur un seul nœud visible `company`, tout en gardant
les identifiants canoniques en métadonnées. Il n'ajoute donc pas de deuxième
graphe technique ni de contrat legacy à maintenir.

La projection visible suit cette grammaire :

| Objet visible | Source et sens |
|---|---|
| `market` | scope régional courant (`TW`, `EU`, `US`) ; conteneur de navigation |
| `domain` | groupe de comparaison transverse, par exemple technologie ou énergie |
| `family` | famille métier locale à un market, plus granulaire que le domain |
| `company` | entreprise affichée ; symbole et instrument sont repliés, identité/géographie liées au World Graph |
| `driver` | facteur typé par `signal_class` (`regime_bundle`, `news_state`, `company_state`, `unspecified`, `missing`) et `source_family` (`macro_observation`, `knowledge_artifact`, `unproven`) |

Un rapport reste une source et un panneau de lecture, **pas un nœud**. Une
observation reste une assertion datée portée par l'association dirigée entre
un driver et sa cible : nature `observed` / `inferred` / `hypothesized`, fenêtre
d'effet courante, fraîcheur et points de preuve. La vue ne transforme jamais
cette association en causalité mesurée.

La projection ne plafonne pas le nombre de drivers associés à une famille :
toute association retenue est matérialisée. Les nœuds driver sont volontairement
plus compacts que les domaines et les familles ; leur cycle temporel règle leur
opacité au lieu de supprimer arbitrairement les facteurs les moins forts.

### Cycle de vie visuel d'un driver

Le read model Desktop porte `activeFrom`, `expectedUntil` et `activity`.
Aujourd'hui, une preuve issue d'un brief utilise `as_of` et `valid_until` comme
**fenêtre de contexte courante** ; ce n'est pas encore une estimation physique
de la durée causale. Sans fenêtre datée, le driver est `Provisional`. Avec une
fenêtre, il passe visuellement de `Active` à `Fading`, puis `Historical` ; son
opacité diminue sans supprimer le nœud. Une sélection le remet en pleine
visibilité pour préserver la navigation.

### Frontière de la temporalité apprise

Aucun learner actuel n'estime la durée d'influence d'un driver. Markov et GRU
apprennent une classe de mouvement aux horizons fixes 4 h, 1 jour et 3 jours ;
les features graphe comptent des relations dans les fenêtres `0-4h`, `4-24h`,
`1-7d` et `older`, avec `decay: none`. Le score `activity` ci-dessus est donc
un seam de présentation alimenté aujourd'hui par la fraîcheur du brief, pas un
résultat appris.

La future capacité shadow devra apprendre une persistance par driver canonique
(`signal_class` + `source_family`) et cible, à partir d'un onset point-in-time et des
`WorldOutcome` arrivés ensuite. Le `valid_until` d'une observation restera une
borne de censure/fraîcheur, jamais une preuve que l'influence s'est arrêtée. Le
read model de cette capacité pourra alors alimenter `activity` et réguler
l'opacité de tous les drivers sans modifier leur présence dans le graphe ni
introduire une relation `CAUSES`.

Les patterns ne sont pas matérialisés dans cette version. Ils pourront émerger
plus tard des épisodes et résultats shadow, puis être publiés comme objets
typés si leur cycle de vie et leur support empirique sont suffisants.

## Owners DDD

| Couche | Rôle |
|---|---|
| Port `WorldGraphExplorerQuery` | contrat de lecture, côté consommateur |
| `WorldGraphExplorerService` | parsers, folds, attestation, PIT, troncature |
| `SqliteWorldGraphExplorerQuery` | SQL URI `mode=ro` uniquement |
| `desktop/bridge/api.py` | composition mince |
| Adaptateur TS `bindWorldGraphProjection` | jointure read-side sans autorité ni écriture |
| `MarketInfluenceGraph` | modèle de présentation typé et temporel du graphe D3 unique |

Le cycle de vie et les règles PIT restent en Python. Le pont Deno ne mappe
que l'URL.

L'adaptateur **n'instancie jamais** `WorldGraphStore` : son constructeur
applique le schéma. Il ne crée, ne migre et n'écrit pas
`state/world_model.db`. Fichier absent → `not_started`. Tables manquantes
ou ledger illisible → `unavailable` (champ optionnel `error`, ce n'est pas
un statut). Révision active absente → `not_started`. Révision publiée
hydratable → `loaded`. Ce schéma n'émet pas `missing` ni `error` comme
`status`.

## Ce qui peut apparaître

Seules les têtes structurelles **liées à la révision publiée active** et les
relations de connaissance dont `ontology_revision` est **exactement** cette
révision. Le graphe projeté est **fermé** sur ces têtes : les extrémités
d'une relation admise (`TRADED_ON`, `MEMBER_OF_FAMILY`, `ISSUED_BY`, …)
apparaissent comme nœuds, même si `entities` de la révision les omet.
Les identifiants de relation et les `source_refs` sont conservés. Les arêtes
parallèles restent distinctes par `relation_id`.

`MEMBER_OF_FAMILY` / `ISSUED_BY` ne sont **pas** inférés depuis la topologie
contexte. Le bootstrap mapping ne publie que `TRADED_ON` / `LOCATED_IN` /
`PART_OF_WORLD`. Ces kinds n'apparaissent ici que s'ils sont des têtes
gelées de la révision active.

Les nœuds overlay (observation, artefact) n'apparaissent que comme extrémités
de relations de connaissance admises. Une troncature déterministe est
déclarée (`truncation`) uniquement quand les plafonds nœuds/arêtes sont
dépassés ; les comptes distinguent le total et le retourné. Omettre une
tête admise parce que son extrémité manquait dans `entities` n'est pas une
troncature.

## Ce qui ne peut pas apparaître

- une relation structurelle ou OBSERVES d'une autre révision (les ABOUT suivent les endpoints, pas le tampon) ;
- une tête structurelle absente de la révision publiée ;
- une arête `CAUSES` / `HYPOTHESIZED_INFLUENCE` ;
- un graphe NetworkX persisté ;
- un cutoff historique choisi par l'URL.
