# RFC — Graphe temporel et hypothèses de patterns du World Model

- **Date** : 2026-08-23
- **Statut** : 💬 RFC proposée — prête pour revue ; aucune activation runtime
- **Auteurs** : Erwan + Codex
- **Portée** : ontologie temporelle, projection NetworkX, features graphe,
  hypothèses de chaînes et outcomes prospectifs
- **Autorité** : `shadow_only` / `NO_GO` ; `decision_effect=none`
- **Registre** : D19 ; cette RFC ne modifie pas l'autorité Trader
- **Dépend de** : [D19](../../decisions/registre-decisions-metier.md),
  [World Model shadow](../../explanation/architecture/world-model-shadow.md),
  [ontologie World Context](../../explanation/architecture/world-context-ontology.md)
- **Supersède** : aucun
- **RFCs sœurs** : [macro source-only](2026-08-23-world-model-macro-source-only-design.md),
  [cohorte prospective](2026-08-23-world-model-prospective-cohort-design.md)

## 1. Résumé et décision proposée

Faire évoluer le graphe actuel de provenance vers une ontologie temporelle
capable de représenter et d'évaluer des sous-graphes tels que :

```text
observation macro -> région/pays <- place <- instrument
                                      |          |-> famille
                                      |          \-> entreprise
                                      \-> autres instruments comparables
instrument cible -> probabilité de mouvement -> outcome à 4 h / 1 j
```

Le graphe ne devient pas un oracle causal. Il décrit : entités, relations,
preuves, temporalité et chemins admissibles à un cutoff. Une chaîne supposée
prédictive devient un objet métier séparé `PatternHypothesis`, puis est évaluée
par des agrégats `PatternOccurrence` et leurs `PatternOutcomeLink` prospectifs.
Elle n'est jamais persistée comme
une arête factuelle `CAUSES`.

La vérité canonique reste constituée d'objets de domaine typés et immuables.
NetworkX construit une projection fraîche pour traverser ou valider ces
objets ; le graphe mutable NetworkX n'est jamais persisté. La première
expérience ML reste un vecteur déterministe compact donné aux voies Markov/GRU
dans une lane graphe distincte. La découverte de chaînes explicites est un
service séparé (`explicit_graph_pattern.v1`) : elle ne couple pas, n'importe
pas et n'entraîne pas de GRU. Ni GNN, ni GraphRAG, ni Neo4j dans cette RFC.

## 2. Ce qui existe et ce qui manque

### 2.1 Déjà implémenté

- `EntityRef`, `TopologyEdge`, `KnowledgeArtifact` et
  `WorldContextSnapshot` sont immuables, hashés et stdlib-only ;
- la projection `WorldContextGraph` crée un `networkx.MultiDiGraph` détaché ;
- la voie contexte relie déjà instrument, venue, famille gelée et, sous garde stricte,
  émetteur vérifié et artefacts sensoriels ;
- les arêtes `CAUSES` sont interdites ;
- la voie contexte est une projection plate : aucune topologie n'est encodée dans
  Markov ou GRU.

### 2.2 Manques à couvrir

- entités `region`, `country`, `macro_indicator` et `event` ;
- identités instrument globalement namespacées par venue ;
- histoire autonome des entités/relations, avec assertion, retrait et
  supersession append-only ;
- relations de connaissance `ABOUT`, `OBSERVES`, `DERIVED_FROM`, `SUPERSEDES` ;
- profil de features graphe explicite, distinct du booléen `include_context` ;
- `PatternHypothesis`, occurrences, outcomes, contre-exemples et contrôles
  négatifs ;
- séparation stricte entre découverte d'un pattern et évaluation prospective ;
- pont futur vers FLAIR/MemRL sans dupliquer leurs stores ou scorers.

## 3. Question produit et limites de claim

Le graphe répond à :

> À `T`, quelles observations prouvées touchent quelles entités, par quels
> chemins structurels versionnés, et quel signal descriptif peut-on en dériver ?

Le modèle dynamique répond ensuite à :

> Les séquences de ces signaux ont-elles amélioré une prévision appariée sur
> des outcomes futurs à horizon fixe ?

Même une chaîne répétée et prédictive ne suffit pas à établir :

- que le premier événement cause le dernier ;
- qu'une action Trader utilisant cette chaîne aurait gagné ;
- que FLAIR/MemRL doivent la rappeler ;
- qu'un modèle doit influencer Brain, Univers, RiskGate ou broker.

Le vocabulaire public reste `predictive_hypothesis`,
`predictive_association_observed`, `not_supported` ou `invalidated`. Le mot
`causal` ne qualifie jamais un résultat de cette RFC.

## 4. Architecture pilotée par les objets métier

### 4.1 Agrégats, value objects et événements

| Objet | Rôle | Cycle de vie |
|---|---|---|
| `WorldEntityRef` | identité namespacée stable | immuable |
| `WorldEntityIdentityMap` | agrégat de correspondances contexte→graphe prouvées | assertion, retrait et supersession par événement |
| `WorldRelation` | union fermée de relation structurelle ou de connaissance | assertion puis retrait/supersession par événement |
| `WorldOntologyRevision` | heads structurels déterministes, jamais les observations dynamiques | publié, puis supersédé |
| `KnowledgeArtifact` | preuve à propos d'entités | append-only, éligibilité dérivée des horloges |
| `WorldContextSnapshot` | vue exacte à un cutoff | immuable |
| `WorldGraphSnapshot` | sous-voie graphe admissible enraciné sur un épisode | immuable |
| `WorldFeatureContract` | contrat de base partagé marché/contexte/graphe et fingerprint | immuable |
| `WorldFeatureMask` | sous-ensemble de groupes du contrat et fingerprint | immuable |
| `MacroGraphBridgeRegistry` | agrégat coordinateur single-active par `bridge_key` | générations activées, bloquées et handoff par events |
| `MacroGraphBridgeRun` | état d'une génération détenue par le registry | actif, bloqué, repris ou supersédé |
| `PatternHypothesis` | agrégat d'une chaîne formulée avant outcome | journal d'événements typés |
| `PatternOccurrence` | agrégat borné d'une application à un cutoff/instrument | journal d'événements typés |
| `PatternOutcomeLink` | lien vers la feuille `WorldOutcome` canonique | append-only/supersédable |
| `PatternAssessment` | support, lift, calibration, contrôles | read model reconstructible |

Le domaine expose les méthodes qui gouvernent les transitions. L'application
orchestre ; le runtime compose ; le store append. Aucun adapter ne fait
`payload["status"] = ...` pour créer un état métier.

### 4.2 Événements de domaine initiaux

```text
WorldEntityAsserted
WorldEntityRetired
WorldEntitySuperseded
WorldEntityIdentityLinked
WorldEntityIdentityUnlinked
WorldEntityIdentityLinkSuperseded
WorldRelationAsserted
WorldRelationRetired
WorldOntologyRevisionPublished
WorldOntologyRevisionSuperseded

PatternHypothesisRegistered
PatternEvaluationStarted
PatternEvaluationClosed
PatternHypothesisInvalidated

PatternOccurrenceRecorded
PatternOutcomeLinked
PatternOutcomeLinkSuperseded
PatternOccurrenceInvalidated
```

`PatternHypothesis` ne charge jamais toutes ses occurrences : son journal reste
borné aux transitions register/start/close/invalidate. Chaque
`PatternOccurrence` est un agrégat séparé, borné à sa création et aux feuilles
d'outcome de ses horizons. Les agrégats refusent : lien outcome avant
occurrence, occurrence antérieure au départ, nouvelle feuille sans
supersession explicite, changement de définition sous le même ID.

## 5. Identités d'entités graphe

Les `EntityRef` contexte restent le scope marché local. Les identités graphe
sont globalement namespacées :

```text
world:market
region:iso-un-m49:030
country:iso-3166:TW
venue:mic:XTAI
family:taxonomy:v1:semiconductors
company:lei:549300...
instrument:mic:XTAI:symbol:2330
macro_indicator:dbnomics:FED/H15/RIFSPFF_N.D
event:provider:event-id
sensor:world_macro_source.v1
```

Les nœuds de connaissance ne sont pas tous des entités du monde. Le domaine
définit une union fermée `WorldGraphNodeRef` :

```text
WorldEntityRef | SensorRef | KnowledgeArtifactRef | WorldObservationRef |
WorldGraphSnapshotRef | PatternHypothesisRef | MacroSourceFactVersionRef
```

Chaque ref possède son propre préfixe/schema/hash. Une relation structurelle
accepte seulement deux `WorldEntityRef`; une relation de connaissance déclare
les variantes source/cible autorisées. Grok ne doit pas inventer des artefacts
en les faisant passer pour `WorldEntityRef`.

Un symbole seul ne constitue jamais un ID global d'instrument. Aucune migration
ne réécrit les refs contexte : l'agrégat `WorldEntityIdentityMap.v1` relie, quand c'est
non ambigu, la référence contexte locale à l'identité graphe namespacée. Chaque lien
porte ses preuves et son intervalle de validité. Il est créé, retiré ou
supersédé uniquement par les événements typés ci-dessus ; une même ref contexte active ne
peut pointer vers deux graphe et une correction ne réécrit jamais l'ancien lien.

Une entreprise n'est créée que depuis un identifiant externe vérifié
namespacé (`lei`, `cik` ou provider issuer ID approuvé). L'ISIN identifie un
instrument, jamais l'entreprise ; un nom d'émetteur ne suffit jamais. Une
entreprise peut émettre plusieurs instruments et un
instrument peut être coté sur une place ; la taxonomie n'est donc pas un arbre
rigide.

`WorldScopeMapping` est l'unique autorité pour la topologie
ancre→venue→country→region→world. Le service de publication dérive
déterministement de ses entrées les heads structurels `TRADED_ON`, `LOCATED_IN`
et `PART_OF_WORLD` concernés ; ni l'identity map ni
`config/world_graph.yaml` ne peuvent en publier une variante. L'identity map
traduit seulement une ref legacy vers une identité graphe. À la publication, une
comparaison canonique exige l'égalité exacte entre les heads dérivés et le
mapping gelé : relation manquante, supplémentaire ou contradictoire = rejet de
la révision.

## 6. Relations temporelles

### 6.1 Structure factuelle

```text
PART_OF_WORLD      region/country/venue -> world
LOCATED_IN         country/venue -> region/country
TRADED_ON          instrument -> venue
ISSUED_BY          instrument -> company
MEMBER_OF_FAMILY   instrument -> family
```

### 6.2 Connaissance et provenance

```text
ABOUT              artifact -> entity
OBSERVES           observation -> entity
DERIVED_FROM       artifact/observation -> artifact/fact
SUPERSEDES         version -> prior version
USES               snapshot/hypothesis -> artifact/observation
```

Les deux familles utilisent des enums/classes distinctes
(`StructuralRelationKind`, `KnowledgeRelationKind`). Une relation
`HypothesizedInfluence` appartient uniquement à `PatternStep`; elle ne peut pas
être insérée dans l'ontologie factuelle.

### 6.3 Bridge prospectif macro → graphe

`MacroScope.entity_id` réutilise les IDs `WorldEntityRef` graphe (`iso-un-m49`,
`iso-3166`, `mic`) ; la conversion depuis les IDs provider est gelée dans
`MacroSourceRegistry`, pas redécouverte à la capture. Après publication et reçu
durable d'un `MacroObservationEnvelope`, le use case
`RegisterMacroObservationKnowledge` crée un `WorldObservationRef` dérivé de
l'`observation_id`, puis demande au ledger l'append d'une relation
`OBSERVES(observation_ref, scope_entity_ref)`.

La construction appartient au domaine et reste pure :
`MacroObservationKnowledgeLink.from_envelope(envelope, scope_mapping,
structural_revision)` reçoit le `WorldScopeMapping` complet, en vérifie le hash,
et exige que `observation.scope` soit `world:market` ou une ref canonique
déclarée parmi les sorties de ce mapping. Elle ne prétend pas résoudre une
ancre marché : `WorldScopeResolution` appartient au slot/snapshot, pas à
`MacroObservationEnvelope`. Son `relation_id` est le hash canonique de son
schema, kind, source, cible, intervalle, révision et refs ;
`effective_from = observation.cutoff_at` et
`effective_until = observation.valid_until`. Les `source_refs` contiennent
exactement l'observation ID/hash, le receipt ID/hash de l'enveloppe et le
mapping ID/hash. Un scope absent du mapping produit un résultat terminal typé
`scope_not_registered`, jamais une relation inventée.

Cette relation `OBSERVES` est une connaissance append-only validée contre une
révision structurelle ; elle n'entre pas dans les heads ni dans le hash figé de
`WorldOntologyRevision`. Une révision structurelle et ses relations dynamiques
restent donc deux histoires distinctes que le snapshot joint au cutoff.

Ce bridge tourne dans le worker macro, hors capture d'épisode. La relation a
son propre reçu store-assigned : elle devient visible uniquement aux cutoffs
futurs postérieurs à son `effective_ready_at`. Un snapshot ne fabrique jamais
ce lien à la volée et ne rétrodate jamais une observation arrivée trop tard.
Replay exact = no-op ; scope inconnu/non namespacé = rejet explicite et
observation encore consultable mais absente du graphe.

L'agrégat append-only `MacroGraphBridgeRegistry`, keyé par `bridge_key`, rend
l'outbox/reconciler durable et possède les générations `MacroGraphBridgeRun`.
Pour la première activation d'une `bridge_key`, le port source
`reserve_activation_cursor(bridge_key, request_id)` append sous le verrou du
ledger macro une `MacroObservationCursorReservation` exactement ordonnée avec
les reçus d'observation. La réservation est fsync, immuable et idempotente par
`request_id`. Le service passe ensuite cette réservation au domaine :
`MacroGraphBridgeRegistry.activate(...)` valide l'absence de génération active
et produit `MacroGraphBridgeActivated`; le ledger ne fait que l'append avec
expected version. Un crash entre réservation et activation réutilise donc la
même frontière au retry ; il ne relit jamais un tail plus récent.

Les événements suivants sont
`MacroGraphObservationLinked`, `MacroGraphObservationSkipped` et
`MacroGraphBridgeCursorAdvanced`. Le skip possède un enum fermé
`scope_not_registered | invalid_envelope`; il est terminal et auditable. Un
mapping ou une révision chargés différents de ceux gelés émettent au niveau du
run, via les méthodes du registry, `MacroGraphBridgeBlocked(reason=config_drift)` :
aucune observation n'est skippée et le cursor n'avance plus.
`MacroGraphBridgeResumed` exige les mêmes ID/hash.

Un changement de mapping, de révision, de plan ou de producteur n'est jamais
migré dans le runtime. Le drift bloque le run et clôt sa capacité d'écriture.
Comme ce contexte reste `shadow_only`, l'opérateur arrête le daemon, archive
hors ligne le store et redémarre sur un store frais portant le contrat courant.
Le runtime ne parse ni ne convertit le contrat archivé.

Chaque travail possède un `MacroGraphBridgeFence(bridge_key, run_id, epoch)`.
L'append du payload relation, la création de son reçu, l'event terminal et
l'avancement du cursor revalident atomiquement ce fence contre le registry
actif et non bloqué dans `world_model.db`. Dès qu'un block est
visible, un ancien worker reçoit `stale_bridge_epoch` et ne peut plus rendre une
relation ou un terminal admissible. Un payload relation commité juste avant le
block mais dont le reçu n'est pas encore créé reste
`availability_unproven`. Une relation déjà reçue reste rattachée au run
bloqué avec sa provenance d'origine ; elle n'est jamais réécrite.

Le reconciler traite strictement en ordre et s'arrête au premier non-terminal :
il n'existe donc aucun terminal au-delà du cursor transmis. Lors d'un cutover,
le journal bloqué reste dans l'archive hors ligne ; le store frais ne reprend
aucune observation de cette ancienne identité.

Le port `MacroObservationScanPort` rescane après restart les enveloppes à reçu
durable, strictement après le cursor d'activation. `MacroObservationCursor` est
une position opaque et immuable du journal de reçus, assignée par le store à
l'append durable (`receipt_log_generation`, ordinal, receipt ID, observation
ID) ; ce n'est ni une datetime ni `effective_ready_at`, et `first_seen_at` ne
peut jamais la déplacer. Pour chaque observation sans événement terminal, le
reconciler réapplique la même factory : relation identique = no-op, contenu
différent sous le même ID = conflit. Il avance le cursor durable seulement
jusqu'au plus grand préfixe contigu dont chaque entrée est `linked` avec reçu
relation durable ou `skipped`; il ne saute jamais une entrée en erreur ou un run
`blocked`.

Un crash entre reçu macro, event relation, reçu relation et event terminal ne
perd donc pas le lien : le retry complète seulement la persistance manquante.
Le scan ne lit jamais un cursor antérieur à celui gelé par l'activation et ne
modifie jamais les horloges de l'observation ou de la relation. Seule la preuve
de disponibilité de la relation autorise sa visibilité point-in-time.

`classify_macro_graph_bridge` compare la spec durable à la spec live
courante (`collection_plan_hash =
7b9d842b4aca42c016fec58c13f1e1f8de305a7432da1183bea391728acecb83`,
`producer_version = world_macro_source.v1`). Run `active` dérivé = block ;
run `blocked` dont la spec diffère = `unknown_drift`. Pas de couple
prédécesseur/successeur, pas de handoff applicatif. Détail :
[RFC macro §17](2026-08-23-world-model-macro-source-only-design.md).

### 6.4 Contrat minimal d'une relation

```json
{
  "schema_version": "world_relation.v1",
  "relation_id": "world_relation:v1:<sha256>",
  "kind": "TRADED_ON",
  "source": {"kind": "instrument", "entity_id": "mic:XTAI:symbol:2330"},
  "target": {"kind": "venue", "entity_id": "mic:XTAI"},
  "effective_from": "2026-01-01T00:00:00Z",
  "effective_until": null,
  "ontology_revision": "market_ontology.v1",
  "source_refs": ["provider:instrument-master:2330"],
  "supersedes": null,
  "content_sha256": "<sha256>"
}
```

Une correction crée une nouvelle assertion et, si nécessaire, un événement de
retrait. Elle ne modifie jamais la relation initiale.

La disponibilité n'est pas un champ caller-controlled de `WorldRelation`.
Les deux ports `append_structural_relation_event()` et
`append_knowledge_relation_event()` retournent une
`PersistedWorldRef[WorldRelationEventId]` et son `AvailabilityEvidence`, créés
après commit/fsync selon le shared kernel de la RFC macro.

## 7. Temporalité et construction à cutoff

Chaque relation/artefact conserve, selon sa nature :

```text
occurred_at       phénomène sous-jacent
published_at      publication explicite de la source
ingested_at       acquisition/validation locale
ready_at          preuve durable de l'enveloppe store-assigned
first_seen_at     première lecture effective de cette preuve
effective_from    début de validité du fait topologique
effective_until   fin exclusive de validité
valid_until       expiration de l'artefact
cutoff_at         instant de construction du snapshot
```

Règle d'admission : l'`AvailabilityEvidence` de l'enveloppe donne
`effective_ready_at = max(ready_at, first_seen_at)`, qui doit être
`<= cutoff_at`; le cutoff doit aussi appartenir aux intervalles de validité.
Une arête vraie aujourd'hui ne peut pas être injectée dans un snapshot ancien.

`WorldOntologyResolver.at_cutoff(cutoff)` charge uniquement les événements
structurels admissibles, applique les supersessions dans un ordre canonique et
retourne une `WorldOntologyRevisionView` immuable. En parallèle,
`WorldKnowledgeResolver.at_cutoff(cutoff, structural_revision)` charge les
relations de connaissance admissibles, validées contre cette même révision.
La projection NetworkX reçoit ces deux vues ; elle ne lit jamais directement
des caches `current`.

`WorldOntologyRevisionPublished` fige les heads d'entités, relations
structurelles et liens d'identité, leurs trois hashes de contenu, ainsi que le
`scope_mapping_id`/`scope_mapping_hash` partagé. Une révision supersédée reste
rejouable ; publier le même ID avec un autre identity-map hash ou un autre
mapping de scopes est un conflit. Les relations `ABOUT`, `OBSERVES`,
`DERIVED_FROM`, `SUPERSEDES` et `USES` n'en modifient jamais le hash.

### 7.1 `WorldGraphSnapshot.v1`

Le snapshot graphe matérialise le sous-graphe exact utilisé pour un épisode :

```json
{
  "schema_version": "world_graph_snapshot.v1",
  "snapshot_id": "world_graph_snapshot:v1:<sha256>",
  "root_episode_id": "world-episode:v1:<sha256>",
  "root_entity": {"kind": "instrument", "entity_id": "mic:XTAI:symbol:2330"},
  "cutoff_at": "2026-08-23T13:00:00Z",
  "ontology_revision": "market_ontology.v1",
  "ontology_hash": "<sha256>",
  "identity_map_hash": "<sha256>",
  "scope_mapping_id": "world_scope_mapping.v1",
  "scope_mapping_hash": "<sha256>",
  "entity_revision_refs": [],
  "identity_link_refs": [],
  "structural_relation_refs": [],
  "knowledge_relation_refs": [],
  "artifact_refs": [],
  "producer_versions": {},
  "traversal_policy_version": "graph_traversal.v1",
  "status": "complete",
  "missingness": {},
  "content_sha256": "<sha256>"
}
```

Tous les membres sont résolus avant le cutoff et référencés par identité/hash.
Le `ontology_hash` couvre seulement la structure gelée ; chaque
`knowledge_relation_ref` porte son propre hash, sa preuve de disponibilité et
la révision structurelle contre laquelle elle a été validée. Le hash du
snapshot couvre les deux ensembles et empêche de confondre overlay dynamique et
ontologie publiée.
Le snapshot n'embarque pas un objet NetworkX. Replay exact = no-op ; même ID
avec contenu différent = conflit ; correction = nouveau snapshot.

Le builder refuse toute révision, résolution, snapshot ou
`WorldCohortSlot` dont le `scope_mapping_id`/`scope_mapping_hash` diffère. Il
recalcule aussi les heads `TRADED_ON`/`LOCATED_IN`/`PART_OF_WORLD` attendus
depuis ce mapping et exige l'égalité canonique avec la révision ; partager un
hash sans partager cette topologie ne suffit pas. Un slot `unmapped` ou
`ambiguous` reste un membre valide avec missingness explicite et sans relation
macro fabriquée ; seul `resolved` autorise le contenu du sous-graphe
correspondant.

## 8. NetworkX : projection, pas autorité

Le choix est maintenu :

- records/domain events = vérité canonique ;
- store append-only = autorité de persistance ;
- resolver = sémantique point-in-time ;
- `nx.MultiDiGraph` frais = traversée et validation ;
- reporting = vue reconstruite.

Les clés d'arêtes NetworkX restent leurs digests pour préserver les relations
parallèles. Muter `copy_backend()` ne change rien au domaine. Aucun pickle de
graphe, aucune table de nœuds mutable faisant autorité, aucun Neo4j ou service
graph externe.

Politique initiale `graph_traversal.v1` : profondeur maximale 4, au plus 32
chemins par racine, aucun cycle, tie-break lexical par `relation_id`, buckets de
fraîcheur `0-4h`, `4-24h`, `1-7d`, `older`. Les signatures de chemin contiennent
uniquement kinds, relations, sens et buckets — jamais les IDs d'instrument ou
d'entreprise comme feature.

Le projecteur conserve le sens canonique de chaque arête, mais la politique de
traversée peut emprunter explicitement une arête en sens inverse. Le owner de
cette algèbre est `world_graph` : il exporte la seule table
`GRAPH_TRAVERSAL_V1_DIRECTIONS`, les kinds de nœuds de chemin (sans `sensor`,
parce qu'aucun endpoint légal ne l'admet) et un validateur d'hop sans IDs.
NetworkX importe cette table ; il ne la recopie pas.

Le validateur vérifie la famille de relation (structurelle ou connaissance),
l'appariement canonique des kinds d'endpoints, le sens (`reverse` échange les
endpoints canoniques) et interdit `CAUSES`. Ainsi une observation
`OBSERVES region` peut rejoindre une place par `LOCATED_IN(reverse)`, puis un
instrument par `TRADED_ON(reverse)` ; depuis cet instrument, `ISSUED_BY` et
`MEMBER_OF_FAMILY` forment deux branches sœurs. Il n'existe aucune arête
inventée `family -> company`, et tout hop hors table ou hors pair canonique
est rejeté.

Graphology peut servir plus tard à une visualisation frontend, mais le frontend
est hors scope et ne devient jamais une source métier.

## 9. Contrat `PatternHypothesis.v1`

Une hypothèse est formulée **avant** ses occurrences d'évaluation :

```json
{
  "schema_version": "pattern_hypothesis.v1",
  "hypothesis_id": "pattern_hypothesis:v1:<sha256>",
  "registered_at": "2026-09-01T00:00:00Z",
  "evaluation_start_not_before": "2026-09-02T00:00:00Z",
  "target": {
    "entity_kind": "instrument",
    "horizon_id": "elapsed_1d.v1",
    "move_distribution": {"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50}
  },
  "steps": [
    {
      "ordinal": 0,
      "source_kind": "world_observation",
      "relation_kind": "OBSERVES",
      "direction": "forward",
      "target_kind": "country",
      "freshness_bucket": "0-4h",
      "evidence_rule_version": "macro_path_rule.v1"
    },
    {
      "ordinal": 1,
      "source_kind": "country",
      "relation_kind": "LOCATED_IN",
      "direction": "reverse",
      "target_kind": "venue",
      "freshness_bucket": "4-24h",
      "evidence_rule_version": "market_ontology.v1"
    }
  ],
  "formation_cutoff": "2026-09-01T00:00:00Z",
  "formation_dataset_fingerprint": "<sha256>",
  "feature_contract_id": "world_feature.graph.v1",
  "feature_contract_fingerprint": "<sha256>",
  "feature_mask_id": "graph_content.v1",
  "feature_mask_fingerprint": "<sha256>",
  "model_identity": "explicit_graph_pattern.v1",
  "ontology_revision": "market_ontology.v1",
  "stats": {
    "support": 7,
    "population_support": 20,
    "class_counts": {"DOWN": 1, "FLAT": 2, "UP": 4},
    "population_class_counts": {"DOWN": 6, "FLAT": 7, "UP": 7},
    "smoothing_alpha": 1.0,
    "association_metric": "total_variation.v1",
    "association_score": 0.12,
    "pattern_distribution": {"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50},
    "population_distribution": {"DOWN": 0.304347826, "FLAT": 0.347826087, "UP": 0.347826087}
  },
  "source_refs": [],
  "causal_claim": false,
  "content_sha256": "<sha256>"
}
```

`steps` sont une projection ID-free de `WorldTemporalPathStep` : kinds, relation
ontologique, sens, bucket de fraîcheur et version de règle de preuve. Ils ne
sont jamais insérés comme arêtes factuelles. Le hop appelle le validateur
canonique de `world_graph`. L'identité d'occurrence exige tous ces champs.

La forme non déployée `subject_kind` / `predicate` / `object_kind` /
`lag_window` est retirée. Les tables live n'ont aucune ligne durable, donc
`pattern_hypothesis.v1` adopte directement la projection corrigée. Il n'y a pas
de contrat v2 ni de couche de compatibilité.

Le `formation_dataset_fingerprint` empêche d'évaluer comme prospective une
hypothèse découverte sur les mêmes outcomes. Il est calculé sur les preuves
triées (épisode, snapshot id/hash, outcome id/hash, ancre temps-spécifique et
signature de chemin) : un changement de label ou de preuve change l'identité.

`stats` est un value object immuable persisté dans `PatternHypothesis.v1`.
Les tables live sont vides : le contrat corrigé remplace directement v1, sans
v2 ni couche legacy. Les comptages sont des ancres marché temps-spécifiques
uniques (`venue`, `symbol`, `bar_interval`, `as_of_bar_ts`, `horizon`) — jamais
des instances brutes de chemins, et jamais tous les timestamps d'un même
symbole fusionnés. `support` est un entier positif ; `population_support >=
support`. Les `class_counts` portent exactement DOWN/FLAT/UP, non négatifs, et
somment `support` ; les comptes population somment `population_support`.
`smoothing_alpha` est fini et `> 0`. La métrique d'association est
exactement `total_variation.v1` (demi-somme des écarts absolus entre les
distributions Laplace-lissées pattern et population), pas une divergence KL.
`target.move_distribution` doit égaler la distribution pattern lissée.

`PatternEvaluationStarted` porte un `evaluation_cohort_id` d'identité
obligatoire. L'agrégat expose `PatternHypothesis.evaluation_cohort_id`.
`PatternOccurrence.record` exige que son `cohort_id` soit exactement cette
cohorte d'évaluation démarrée. Aucun défaut de compatibilité : les tables live
n'ont aucune ligne.

### 9.1 Cycle de vie

```text
registered -> evaluating -> evaluation_closed
      \              \----> invalidated
       \------------------> invalidated
```

La définition est immuable après `registered`. Une variation de chaîne,
horizon, seuil, population, contrat/mask de features ou modèle crée un nouvel
ID. Fermer l'évaluation
n'empêche pas une correction d'outcome append-only ; elle produit une nouvelle
version de `PatternAssessment` et garde l'ancienne.

## 10. Occurrences, outcomes et assessment

### 10.1 `PatternOccurrence.v1`

Une occurrence référence : hypothèse, cohorte, instrument namespacé, cutoff,
chemin exact, artefacts/faits utilisés, IDs et fingerprints du contrat et du
mask, prédiction et IDs d'horizons attendus. Ces quatre champs doivent être
identiques à ceux de l'hypothèse et de la lane de cohorte. Elle exclut
`ready_at` de son payload ; le ledger
retourne son `AvailabilityEvidence` après persistance, obligatoirement avant le
target label. Le matching prospectif fixe
`expected_horizon_ids = (elapsed_4h.v1, elapsed_1d.v1, elapsed_3d.v1)`
alors que la prévision shadow n'en porte qu'un (`PatternForecast` /
`WorldPrediction`). L'occurrence est récupérable avant tout outcome.

### 10.2 `PatternOutcomeLink.v1`

```json
{
  "schema_version": "pattern_outcome_link.v1",
  "link_id": "pattern_outcome_link:v1:<sha256>",
  "occurrence_id": "pattern_occurrence:v1:<sha256>",
  "horizon_id": "elapsed_1d.v1",
  "world_outcome_event_id": "world-outcome:v1:<sha256>",
  "world_outcome_content_sha256": "<sha256>",
  "supersedes_link_id": null,
  "content_sha256": "<sha256>"
}
```

Le link ne recopie ni `move_class`, ni rendement, ni timestamps du label. Le
read model charge la feuille `WorldOutcome` canonique, vérifie ID/digest/horizon
et lit sa sémantique. Une correction crée un nouveau link vers le nouvel event
et supersède l'ancien ; jamais d'overwrite ni de deuxième autorité du label.
Un replay du même leaf actif est idempotent. Une correction canonique dont
`supersedes_event_id` pointe le leaf actuellement lié passe par
`WorldPatternService.link`. Un saut de chaîne (C qui supersède B alors que A
est lié) reste rejeté : les contrôles `supersedes` explicites ne sont pas
assouplis. Seul `PatternOutcomeLinkService` lit les feuilles actives ; le
matching n'inspecte aucune ligne d'outcome. Un horizon encore absent reste
pending.

### 10.3 `PatternAssessment.v1`

Read model par hypothèse/version : support unique, couverture, log-loss/Brier,
calibration, lift contre contrôle contexte, contextes favorables, contre-exemples,
résultats des permutations et raisons d'exclusion. Ses conclusions restent
`predictive_association_observed`, `inconclusive`, `not_supported` ou
`invalidated`.

## 11. Profil de features graphe

Le booléen `include_context` ne doit pas devenir deux ou trois booléens. La voie graphe
étend le type canonique `WorldFeatureContract` défini par la RFC cohorte :

```text
contract_id
accepted_episode_contract
projection_version
encoder_identity
allowed_feature_groups
ontology_revision
path_rule_version
windows_and_decay
vocabulary_fingerprint
```

Le module owner reste `trader/domain/world_feature_contract.py`. Cette RFC
n'introduit aucun `FeatureContractSpec` concurrent. Les masks graphe
(`topology_status_only`, contenu topologique complet, etc.) restent des
`WorldFeatureMask` séparés ; une lane et une prédiction persistent le
fingerprint du contrat **et** celui du mask.

Profils :

| Profil | Contrat | Contenu |
|---|---|---|
| marché | `world_feature.market.v1` | marché |
| contexte | `world_feature.context.v1` | marché + contexte plat |
| graphe | `world_feature.graph.v1` | contexte + features de chemins déterministes |

Le premier graphe reste petit :

- statut/couverture de résolution du graphe ;
- nombre de sources et artefacts admissibles par fenêtre ;
- nombre de chemins admissibles par type et profondeur ;
- profondeur minimale et fraîcheur maximale/minimale bornées ;
- signatures de chemins dans un vocabulaire fermé/hashé ;
- accord/désaccord de signaux macro sur un même scope, si les sources existent ;
- indicateurs explicites de missingness.

Exclusions initiales : texte brut, embeddings de rapports, centralité globale,
PageRank, community detection, message passing appris, features calculées sur
le graphe courant plutôt qu'au cutoff.

Les fenêtres, decay, profondeur maximale et vocabulaire sont gelés dans le
fingerprint. Recommandation initiale : chemins structurels de profondeur
`<= 4`, sans parcours cyclique, et agrégats calculés uniquement sur les
artefacts référencés par le snapshot.

## 12. Expérience graphe et découverte de patterns

### 12.1 Ablation prospective

La voie graphe commence dans une nouvelle cohorte définie par la
[RFC cohorte](2026-08-23-world-model-prospective-cohort-design.md) :

```text
market
flat context
topology_status_only       # contrôle processus/topologie
graph_content              # chemins + contenu structuré
```

Markov graphe et GRU graphe reçoivent chacun une identité distincte. Ils apprennent à
zéro sur les mêmes slots/labels ordonnés que leurs contrôles. Le graphe
NetworkX lui-même n'est pas l'entrée ; seul `WorldFeatureContract` graphe produit
un vecteur déterministe.

### 12.2 Découverte puis confirmation

1. un pilote graphe collecte le **compagnon** `WorldEpisode` graphe
   (`world_feature.graph.v1`), ses outcomes, et le snapshot embarqué dans
   `observation.graph` / `graph_features.snapshot`.
   `WorldGraphSnapshot.root_episode_id` reste l'épisode **marché** sous-jacent
   et est volontairement distinct de l'épisode graphe. Un record de formation
   exige l'égalité `outcome.episode_id` / épisode graphe et
   `embedded snapshot_id == snapshot.snapshot_id` ; il n'identifie jamais le
   snapshot par l'ID de l'épisode graphe. Des lignes ancre/provenance
   dupliquées avec des labels DOWN/FLAT/UP contradictoires sont rejetées
   (pas d'overwrite silencieux) ; une preuve identique peut être dédupliquée ;
2. `PatternDiscoveryService` projette des `PatternStep` (aucun second type de hop)
   depuis l'ancestralité structurelle unique en avant — `TRADED_ON` vers la
   venue, `LOCATED_IN` pays/région, `PART_OF_WORLD` vers world — plus les
   branches racine `ISSUED_BY` / `MEMBER_OF_FAMILY`. Il n' inverse jamais
   `TRADED_ON` vers des instruments frères et n'appelle pas NetworkX ;
3. chaque overlay `OBSERVES`/`ABOUT` incident à un nœud visité prolonge la
   chaîne jusqu'à la preuve (en général en reverse). Les overlays extérieurs
   sont exclus. Les buckets de fraîcheur viennent de `effective_from` contre
   `snapshot.cutoff_at`. `evidence_rule_version` est `GRAPH_PATH_RULE_VERSION`
   pour tous les hops courants ;
4. le groupement est (steps sémantiques, horizon, contrat/mask + fingerprints,
   révision d'ontologie, `explicit_graph_pattern.v1`). La population est
   l'ensemble des ancres temps-spécifiques éligibles de même horizon/provenance ;
5. les candidats sont rangés par association desc, support desc, signature
   asc, horizon asc, puis filtrés. Zéro candidat est un résultat valide ;
6. `casys-trader world pattern discover` est un dry-run. `--apply` enregistre
   puis démarre l'évaluation (`registered` → `evaluating`) sur une
   `evaluation_cohort_id` et un `evaluation_dataset_fingerprint` distincts
   du dataset de formation ;
7. `casys-trader world pattern evaluate --as-of …` charge uniquement les
   hypothèses `evaluating` de la `evaluation_cohort_id` demandée, scanne
   uniquement les compagnons graphe référencés par `world_cohort_slots` de
   cette cohorte (clé `world_feature.graph.v1`) et visibles à `as_of`,
   **strictement après** `formation_cutoff` et `evaluation_start_not_before`.
   Une cohorte vide ou absente ne retombe pas sur d'autres épisodes. Le
   `evaluation_dataset_fingerprint` reste la clôture request/hypothèses. Il
   projette les chemins via le module partagé `pattern_path.py`, et construit
   des `WorldPrediction` shadow + `PatternOccurrence` **avant** toute lecture
   de label. Horizons attendus : `elapsed_4h.v1`, `elapsed_1d.v1`,
   `elapsed_3d.v1`. Dry-run par défaut ; `--apply` persiste via
   `WorldModelStore.append_prediction` puis `WorldPatternService.record` ;
8. `casys-trader world pattern link-outcomes --as-of …` est la seule phase
   autorisée à lire les feuilles `WorldOutcome` actives. Elle lie
   séparément 4h / 1d / 3d. Un replay du même leaf actif est idempotent ;
   une correction canonique dont `supersedes_event_id` pointe le leaf
   actuellement lié passe par `WorldPatternService.link`. Un horizon
   manquant reste `pending` ; rien n'est fabriqué. Dry-run par défaut ;
   `--apply` passe par `WorldPatternService.link` ;
9. `casys-trader world pattern status [cohort_id]` est hypothèse-premier :
   une hypothèse `evaluating` reste visible avec zéro occurrence.
   `report` reste le read model d'assessment GRAPH-11.

La projection de chemins n'appartient plus en privé à la découverte :
`project_pattern_paths` / `paths_matching_steps` sont partagés. NetworkX
n'est jamais appelé. Les `evidence_refs` restent hors de
`PatternStep.identity_tuple()`.

Il est interdit de découvrir et confirmer sur les mêmes outcomes. Le matching
ne `SELECT` jamais `world_outcome_events`. La découverte n'émet aucun claim
causal et n'emprunte pas le vocabulaire d'autorité runtime. Aucun câblage
daemon dans ce lot : le World Model reste `shadow_only` / `NO_GO`.

### 12.3 Contrôles négatifs

- permutation des entités dans une même venue/session ;
- décalage temporel respectant les blocs mais cassant l'ordre supposé ;
- `topology_status_only` pour isoler la missingness ;
- chaîne tronquée sans le facteur supposé ;
- population comparable sans l'événement ;
- correction pour multiplicité des hypothèses testées, méthode gelée avant la
  période de confirmation.

## 13. Relation avec FLAIR et MemRL

Cette RFC ne modifie pas les learnings actuels.

- **FLAIR futur** : peut utiliser `PatternAssessment` pour pondérer la qualité
  prédictive historique d'une chaîne, après outcomes prospectifs.
- **MemRL futur** : ne reçoit une reward que si un pattern a été effectivement
  rappelé/cité dans une décision, avec trace pré-décision et outcome de cette
  décision. Une bonne prévision World Model ne prouve pas cette utilité.
- le texte rendu au Brain reste une projection bornée d'un pattern validé ; le
  graphe, ses IDs et son historique complet restent pull-only.

Un adapter futur peut exposer ces objets aux stores existants, mais ne
réimplémente ni `outcome_score`, ni Q-value, ni retrieval. Il exige une RFC ou
décision séparée parce qu'il créerait un nouveau lien avec le chemin Trader.

## 14. Owners DDD et persistance

| Couche | Owner proposé | Responsabilité |
|---|---|---|
| Domaine actuel | `trader/domain/world_context.py` | records contexte et compatibilité |
| Domaine graphe | `trader/domain/world_graph.py` | entités, relations, révisions, snapshots, events |
| Domaine patterns | `trader/domain/world_pattern.py` | agrégats hypothesis/occurrence et lifecycle |
| Application | `ontology_service.py`, `graph_observation_bridge.py`, `graph_ports.py` | commands/queries point-in-time, bridge et ports consumer-owned |
| Application ML | `graph_features.py`, `pattern_service.py`, `pattern_ports.py`, `pattern_path.py`, `pattern_discovery.py`, `pattern_evaluation.py`, `pattern_outcome_link.py` | profiles, features, découverte unlabeled, matching prospectif, liaison d'outcomes |
| Infrastructure graph | `trader/infrastructure/graph/world_temporal_networkx.py` | projection fraîche graphe uniquement |
| Infrastructure état | `world_graph_store.py`, `world_pattern_store.py` | ledgers append-only séparés |
| Reporting | `trader/reporting/read_models/world_patterns.py` | assessments et explications |
| Interface | `trader/interfaces/cli/world_model.py` | façade mince |

Tables proposées dans `state/world_model.db` :

```text
world_entity_events
world_entity_identity_events
world_relation_events
world_ontology_revisions
world_graph_snapshots
world_graph_snapshot_members
world_macro_graph_bridge_events
world_pattern_hypothesis_events
world_pattern_occurrence_events
world_pattern_outcome_links
world_availability_receipts
```

Toutes refusent `UPDATE`/`DELETE`. Les payloads sont hashés ; les identités,
cutoffs, cohortes et versions utiles aux jointures sont aussi des colonnes
indexées qui priment sur le JSON.

Chaque event/snapshot est d'abord commité dans sa table d'autorité, puis son
reçu est commité dans `world_availability_receipts` selon le protocole partagé.
Les `*EventEnvelope` sont reconstruits à la lecture ; ils ne sont jamais
mensongèrement embarqués dans le premier commit. Après crash/restart, un objet
sans reçu reste `availability_unproven` et le retry du reçu est idempotent.

Ports consumer-owned :

```python
class WorldGraphLedger(Protocol):
    def append_entity_event(
        self, event: WorldEntityEvent
    ) -> PersistedWorldRef[WorldEntityEventId]: ...
    def append_structural_relation_event(
        self, event: StructuralWorldRelationEvent
    ) -> PersistedWorldRef[WorldRelationEventId]: ...
    def append_knowledge_relation_event(
        self,
        event: KnowledgeWorldRelationEvent,
        fence: MacroGraphBridgeFence,
        expected_registry_version: int,
    ) -> PersistedWorldRef[WorldRelationEventId]: ...
    def append_identity_event(
        self, event: WorldEntityIdentityEvent
    ) -> PersistedWorldRef[WorldEntityIdentityEventId]: ...
    def append_revision_event(
        self, event: WorldOntologyRevisionEvent
    ) -> PersistedWorldRef[WorldOntologyRevisionEventId]: ...
    def list_entity_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldEntityEventEnvelope, ...]: ...
    def list_identity_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldEntityIdentityEventEnvelope, ...]: ...
    def list_structural_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]: ...
    def list_knowledge_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]: ...
    def list_revision_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldOntologyRevisionEventEnvelope, ...]: ...

class WorldGraphSnapshotLedger(Protocol):
    def append(
        self, snapshot: WorldGraphSnapshot
    ) -> PersistedWorldRef[WorldGraphSnapshotId]: ...
    def get(self, snapshot_id: WorldGraphSnapshotId) -> WorldGraphSnapshot | None: ...

class MacroObservationScanPort(Protocol):
    def reserve_activation_cursor(
        self, bridge_key: MacroGraphBridgeKey, request_id: BridgeRequestId
    ) -> MacroObservationCursorReservation: ...
    def list_available_after(
        self, cursor: MacroObservationCursor, limit: int
    ) -> tuple[MacroObservationEnvelope, ...]: ...

class MacroGraphBridgeLedger(Protocol):
    def append_event(
        self,
        event: MacroGraphBridgeEvent,
        expected_registry_version: int,
        fence: MacroGraphBridgeFence | None,
    ) -> PersistedWorldRef[MacroGraphBridgeEventId]: ...
    def load(self, bridge_key: MacroGraphBridgeKey) -> MacroGraphBridgeRegistry: ...

class WorldPatternLedger(Protocol):
    def append_hypothesis_event(
        self, event: PatternHypothesisEvent
    ) -> PersistedWorldRef[PatternHypothesisEventId]: ...
    def append_occurrence_event(
        self, event: PatternOccurrenceEvent
    ) -> PersistedWorldRef[PatternOccurrenceEventId]: ...
    def load_hypothesis(self, hypothesis_id: PatternHypothesisId) -> PatternHypothesis: ...
    def load_occurrence(self, occurrence_id: PatternOccurrenceId) -> PatternOccurrence: ...
```

Les cinq lectures du ledger retournent uniquement des events typés et leurs
preuves ; elles n'appliquent aucune supersession et ne fabriquent aucune vue.
`WorldOntologyResolver` application-owned est l'unique auteur de
`WorldOntologyRevisionView` et `WorldEntityIdentityMap` à cutoff ;
`WorldKnowledgeResolver` est l'unique auteur de l'overlay de connaissance PIT.

Les use cases reçoivent et retournent ces types. Le resolver, NetworkX et
SQLite implémentent des ports ; ils ne deviennent pas les auteurs du cycle de
vie. Les ports vivent dans `graph_ports.py` et `pattern_ports.py`, côté
application ; application n'importe jamais
`world_graph_store.py`, NetworkX, runtime ou reporting. Les tests package-layout
verrouillent cette frontière.

## 15. Runtime, budgets et rollout

Le resolver et le projecteur graphe font uniquement de l'I/O locale. Aucun fetch
ni LLM pendant la capture. Les traversées sont bornées par instrument,
profondeur, nombre de chemins et cutoff ; un dépassement produit
`graph_budget_exceeded`/missingness et laisse marché/contexte continuer.

Flag proposé, défaut off et boot-only :

```text
CASYS_WORLD_MODEL_GRAPH_ENABLED=0
```

| Phase | Travail | Peut avancer quand |
|---|---|---|
| G0 | contrats/IDs/events/store | dès validation RFC |
| G1 | collecte topologie et résolution PIT | en parallèle du flux macro |
| G2 | read model + projection NetworkX | après G1 |
| G3 | `WorldFeatureContract` graphe + spike offline | contrats macro/cohorte gelés |
| G4 | cohorte pilote graphe | cohorte et contrôles prêts |
| G5 | hypothèses + période de confirmation | support pilote suffisant |
| G6 | adapter FLAIR/MemRL | décision séparée après preuve |

L'implémentation n'active aucun flag, ne modifie pas `.env`, ne redémarre pas
le daemon et ne touche pas au frontend.

## 16. Gates et critères de stop

Stop/invalidation si : ID instrument non namespacé admis en graphe, relation sans
source ou sans `AvailabilityEvidence` store-assigned, arête causale factuelle,
snapshot construit depuis `current`,
path post-cutoff, mutation historique, feature hors profil, découverte et
confirmation sur les mêmes données, model lineage non appariée, ou effet sur
le chemin Trader.

Lancement graphe `NO_GO` tant que :

- macro source-only et manifeste de cohorte ne sont pas stables ;
- les mappings région/pays/place n'ont pas version/hash ;
- les identities entreprise ne sont pas vérifiées ;
- le contrôle `topology_status_only` n'existe pas ;
- les tests de permutations et restart ne passent pas.

## 17. Tests obligatoires

### Domaine/ontologie

- IDs namespacés, enums fermées, immutabilité/hash ;
- symbole homonyme sur deux venues = deux instruments ;
- société multi-instrument et instrument sans issuer vérifié ;
- identity link contexte→graphe prouvé, ambiguïté refusée et correction supersédée ;
- assertion/retrait/supersession et intervalles exclusifs ;
- `CAUSES` impossible dans toute relation factuelle ;
- reconstruction identique quel que soit l'ordre d'append.

### Point-in-time/projection

- relation future invisible ; late/unproven invisible ;
- correction non rétroactive ; restart conservateur ;
- crash entre event/snapshot et reçu puis retry idempotent ;
- NetworkX frais, arêtes parallèles, mutation détachée ;
- path borné, cycle et budget ;
- aucun objet NetworkX dans un payload persistant.

### Feature contracts/modèles

- marché/contexte fingerprints et prédictions non régressés ;
- graphe refuse épisode/profil incompatible ;
- encoder graphe déterministe et borné ;
- `topology_status_only` sans contenu ;
- même slot/labels/lineage entre contexte/graphe ;
- Markov et GRU n'importent ni NetworkX ni l'un l'autre.

### Patterns

- hash d'une chaîne ordonnée ; permutation des steps = autre ID ;
- `PatternFormationStats` : Laplace/TV, roundtrip, identité v1 ;
- `evaluation_cohort_id` requis au start et recopié sur l'occurrence ;
- scan d'évaluation borné aux refs graphe de `world_cohort_slots` ;
- transitions d'agrégat autorisées/interdites ;
- occurrence persistée avant outcome ;
- correction par supersession ;
- replay de link idempotent pour le même leaf actif ;
- formation cutoff antérieur à évaluation ;
- rejet même dataset pour découverte/confirmation ;
- projection : pas de frères TRADED_ON reverse, pas d'overlays extérieurs ;
- comptage par ancre temps-spécifique ; labels tardifs PIT rejetés ;
- ranking déterministe ; provenance non mélangée ; fingerprint sensible aux preuves ;
- aucun import GRU/NetworkX/infrastructure dans la découverte ;
- contre-exemples, multiplicité et contrôles visibles dans assessment.

### Architecture/reporting/runtime

- règles d'import DDD ; CLI sans SQL ; status/report read-only ;
- schéma frais unique ; store d'une autre version rejeté ; triggers anti-mutation ;
- panne/budget graphe sans effet sur marché/contexte/Trader ;
- invariants `shadow_only`, `NO_GO`, `decision_effect=none`.

## 18. Lots Grok CLI bornés

Interdits pour **tous** les lots : `desktop/**`, `.env`, `state/**`, frontend,
restart/kill daemon, push, dépendance graph serveur/GNN, fichier non listé dans
`allowed_edits`, et `docs/reference/world-model.md` tant que son autre scope est
sale.

### Lot GRAPH-1 — domaine graphe

- **depends_on** : `MACRO-0` (availability shared kernel).
- **read_only_context** : `trader/domain/world_context.py`,
  `trader/domain/world_scope.py`, présente RFC.
- **allowed_edits** : `trader/domain/world_graph.py`,
  `tests/domain/test_world_graph.py`.
- **sortie** : refs namespacées, union node refs, agrégat et events d'identity
  map contexte→graphe, events structurels et de connaissance distincts,
  revision events et `WorldGraphSnapshot` avec les deux jeux de refs.
- **tests** : `uv run pytest -q tests/domain/test_world_graph.py
  tests/domain/test_world_availability.py`.
- **exit** : temporalité, correction, lifecycle et interdiction `CAUSES`
  testés ; voie contexte inchangée.

### Lot GRAPH-2 — ports et resolver application

- **depends_on** : `GRAPH-1`.
- **read_only_context** :
  `trader/application/world_model/context_capture.py`.
- **allowed_edits** : `trader/application/world_model/ontology_service.py`,
  `trader/application/world_model/graph_ports.py`,
  `tests/application/test_world_ontology_service.py`.
- **sortie** : ports consumer-owned, commands entity/identity/relation/revision,
  resolver structurel PIT et resolver d'overlay connaissance PIT distincts.
- **tests** : `uv run pytest -q
  tests/application/test_world_ontology_service.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : aucune importation store/NetworkX/runtime/reporting ; correction
  non rétroactive.

### Lot GRAPH-3 — ledger graphe et snapshots

- **depends_on** : `GRAPH-1`, `GRAPH-2`, `COHORT-5` ; cette dépendance
  sérialise migration et edits du store partagé après l'intégration cohorte,
  et réutilise la table de reçus livrée par COHORT-3.
- **read_only_context** :
  `trader/infrastructure/state_db/world_model_store.py`.
- **allowed_edits** : `trader/infrastructure/state_db/world_graph_store.py`,
  `trader/infrastructure/state_db/world_model_store.py`,
  `tests/state_db/test_world_graph_store.py`,
  `tests/infrastructure/test_world_model_store.py`.
- **migration** : prochain ID libre **après** la migration `COHORT-3` ;
  entity/identity/relation/revision events, `world_graph_snapshots`,
  `world_graph_snapshot_members`, réutilisation de
  `world_availability_receipts`, triggers anti-mutation.
- **tests** : `uv run pytest -q tests/state_db/test_world_graph_store.py
  tests/infrastructure/test_world_model_store.py`.
- **exit** : receipts store-assigned, replay/conflit/concurrence/restart et
  snapshot membership prouvés.

### Lot GRAPH-3B — bridge prospectif macro → graphe

- **depends_on** : `GRAPH-2`, `GRAPH-3`, `MACRO-6`.
- **read_only_context** : `trader/domain/world_macro.py`,
  `trader/domain/world_scope.py`,
  `trader/application/world_model/macro_ports.py`.
- **allowed_edits** :
  `trader/domain/world_graph.py`,
  `trader/application/world_model/graph_observation_bridge.py`,
  `trader/application/world_model/graph_ports.py`,
  `trader/infrastructure/state_db/world_graph_store.py`,
  `trader/infrastructure/state_db/world_macro_store.py`,
  `trader/infrastructure/state_db/world_model_store.py`,
  `trader/runtime/world_macro_runtime.py`,
  `tests/domain/test_world_graph.py`,
  `tests/application/test_world_graph_observation_bridge.py`,
  `tests/state_db/test_world_graph_store.py`,
  `tests/state_db/test_world_macro_store.py`,
  `tests/infrastructure/test_world_model_store.py`,
  `tests/runtime/test_world_macro_runtime.py`.
- **sortie** : `RegisterMacroObservationKnowledge`, mapping scope canonique,
  factory pure `MacroObservationKnowledgeLink`, append prospectif de `OBSERVES`,
  agrégat/outbox durable `MacroGraphBridgeRegistry`, générations
  `MacroGraphBridgeRun`, cursor ordinal store-assigned, réservation source
  idempotente, fence epoch, handoff single-active, blocage `config_drift` et
  retry idempotent.
- **migration** : prochain ID libre après `GRAPH-3` ; table append-only
  `world_macro_graph_bridge_events`, reçus partagés, plus journal source
  append-only `state/world_macro/observations/cursor_reservations/` ; aucun
  checkpoint mutable.
- **tests** : `uv run pytest -q
  tests/domain/test_world_graph.py
  tests/application/test_world_graph_observation_bridge.py
  tests/state_db/test_world_graph_store.py
  tests/state_db/test_world_macro_store.py
  tests/infrastructure/test_world_model_store.py
  tests/runtime/test_world_macro_runtime.py`.
- **exit** : aucune relation créée à la capture ; relation post-receipt jamais
  visible à un cutoff antérieur ; ID/intervalle/source refs déterministes ;
  reprise des crashes avant/après réservation, event relation, reçu relation et
  event terminal ; aucun saut d'entrée ni backfill antérieur au cursor
  d'activation ; activation retry sans gap ; handoff sans gap/double ownership ;
  ancien worker fenced avant payload/reçu/terminal/cursor ; skips terminaux ;
  config drift bloque sans avancer ; scope local/non namespacé rejeté ; flag
  défaut off et aucun restart.

### Lot GRAPH-4 — projection NetworkX temporelle

- **depends_on** : `GRAPH-1`, `GRAPH-2`.
- **read_only_context** :
  `trader/infrastructure/graph/world_context_networkx.py`.
- **allowed_edits** :
  `trader/infrastructure/graph/world_temporal_networkx.py`,
  `tests/infrastructure/test_world_temporal_networkx.py`,
  `tests/package_layout/test_infrastructure_layout.py`.
- **sortie** : projection fraîche, cutoff, paths déterministes/bornés.
- **tests** : `uv run pytest -q
  tests/infrastructure/test_world_temporal_networkx.py
  tests/infrastructure/test_world_context_networkx.py
  tests/package_layout/test_infrastructure_layout.py`.
- **exit** : aucune persistance NetworkX, mutation détachée, voie contexte non régressée.

### Gate GRAPH-CONFIG — décision opérateur, non délégable

Avant `GRAPH-5`, un opérateur doit valider et committer
`config/world_graph.yaml`. Cet artefact versionné/hashé référence
obligatoirement l'ID/hash de `config/world_scope_mapping.yaml` ; il ne définit
jamais un second mapping ni les relations de scope qui en sont dérivées. Il
fige taxonomies region/country, providers de
mappings instrument/issuer, directions de traversée, fenêtres/decay, profondeur
et budget de paths, vocabulaire des signatures et contrôles négatifs. Avant
`GRAPH-7`, il doit aussi référencer le `cohort_id` et la règle de
fermeture/multiplicité approuvés. Grok peut valider le schéma mais ne choisit
aucune valeur ; champ/hash manquant =
`blocked_until`, `NO_GO` sans édition.

### Lot GRAPH-5 — profil de features graphe

- **depends_on** : `COHORT-4`, `MACRO-5`, `GRAPH-1`, `GRAPH-CONFIG` ; cette
  dépendance sérialise les contracts/encoders partagés après l'extension macro.
- **read_only_context** : `trader/domain/world_feature_contract.py`,
  `config/world_graph.yaml`, `config/world_scope_mapping.yaml`.
- **allowed_edits** : `trader/domain/world_feature_contract.py`,
  `trader/application/world_model/encoding.py`,
  `trader/application/world_model/baseline.py`,
  `trader/application/world_model/gru.py`,
  `tests/domain/test_world_feature_contract.py`,
  `tests/application/test_world_model_feature_contracts.py`,
  `tests/application/test_world_baseline.py`,
  `tests/application/test_world_gru.py`.
- **sortie** : `world_feature.graph.v1`, model/encoder identities et profil
  `topology_status_only`.
- **tests** : `uv run pytest -q
  tests/domain/test_world_feature_contract.py
  tests/application/test_world_model_feature_contracts.py
  tests/application/test_world_baseline.py tests/application/test_world_gru.py`.
- **exit** : marché/contexte bit/fingerprint compatibles ; graphe isolé, sans NetworkX dans
  les modèles.

### Lot GRAPH-6 — snapshot et features application

- **depends_on** : `GRAPH-2`, `GRAPH-3`, `GRAPH-3B`, `GRAPH-4`, `GRAPH-5`,
  `MACRO-5`.
- **read_only_context** : `trader/domain/world_episode.py`.
- **allowed_edits** : `trader/application/world_model/graph_snapshot.py`,
  `trader/application/world_model/graph_features.py`,
  `trader/application/world_model/graph_ports.py`,
  `tests/application/test_world_graph_snapshot.py`,
  `tests/application/test_world_graph_features.py`.
- **sortie** : snapshot graphe PIT et vecteur borné avec provenance par feature.
- **tests** : `uv run pytest -q
  tests/application/test_world_graph_snapshot.py
  tests/application/test_world_graph_features.py`.
- **exit** : même input/cutoff = même snapshot/features ; budgets, late data et
  missingness contrôlés ; aucune I/O réseau/LLM.

### Lot GRAPH-7 — épisodes et lanes graphe

- **depends_on** : `GRAPH-3`, `GRAPH-5`, `GRAPH-6`, `COHORT-5`,
  `GRAPH-CONFIG` avec `cohort_id` gelé.
- **read_only_context** : `trader/application/world_model/context_capture.py`.
- **allowed_edits** : `trader/domain/world_episode.py`,
  `trader/application/world_model/graph_capture.py`,
  `trader/application/world_model/service.py`,
  `trader/infrastructure/state_db/world_model_store.py`,
  `tests/application/test_world_graph_capture.py`,
  `tests/application/test_world_context_lanes.py`,
  `tests/infrastructure/test_world_model_store.py`.
- **sortie** : canonical-first-write graphe, Markov/GRU graph.v1 et appariement de
  slot marché/contexte/graphe.
- **tests** : `uv run pytest -q tests/application/test_world_graph_capture.py
  tests/application/test_world_context_lanes.py
  tests/infrastructure/test_world_model_store.py`.
- **exit** : chaque profil refuse les autres contracts ; graphe missing neutre ;
  aucune lane runtime activée.

### Lot GRAPH-8 — domaine patterns

- **depends_on** : `GRAPH-1`, `MACRO-0`, `COHORT-0`.
- **read_only_context** : `trader/domain/world_episode.py`,
  `trader/domain/world_feature_contract.py`.
- **allowed_edits** : `trader/domain/world_pattern.py`,
  `tests/domain/test_world_pattern.py`.
- **sortie** : agrégats hypothesis/occurrence séparés, events,
  `PatternOutcomeLink` vers `WorldOutcome`.
- **tests** : `uv run pytest -q tests/domain/test_world_pattern.py`.
- **exit** : journaux bornés, transitions et supersession testées ; aucun label
  dupliqué ni confirmation in-sample.

### Lot GRAPH-9 — service patterns et ports

- **depends_on** : `GRAPH-7`, `GRAPH-8`.
- **read_only_context** :
  `trader/application/world_model/labeler.py`.
- **allowed_edits** : `trader/application/world_model/pattern_service.py`,
  `trader/application/world_model/pattern_ports.py`,
  `tests/application/test_world_pattern_service.py`.
- **sortie** : commands register/start/record/link/close/invalidate et validation
  de la feuille WorldOutcome.
- **tests** : `uv run pytest -q
  tests/application/test_world_pattern_service.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : ports avant adapters ; occurrence durable avant outcome link.

### Lot GRAPH-10 — store patterns

- **depends_on** : `GRAPH-8`, `GRAPH-9`.
- **read_only_context** :
  `trader/infrastructure/state_db/world_model_store.py`.
- **allowed_edits** : `trader/infrastructure/state_db/world_pattern_store.py`,
  `trader/infrastructure/state_db/world_model_store.py`,
  `tests/state_db/test_world_pattern_store.py`,
  `tests/infrastructure/test_world_model_store.py`.
- **migration** : hypothesis/events, occurrence/events, outcome links, indexes
  de cohorte/cutoff/horizon et triggers anti-mutation.
- **tests** : `uv run pytest -q tests/state_db/test_world_pattern_store.py
  tests/infrastructure/test_world_model_store.py`.
- **exit** : replay/conflit/correction/restart et availability evidence prouvés.

### Lot GRAPH-11 — assessment et contrôles négatifs

- **depends_on** : `GRAPH-7`, `GRAPH-9`, `GRAPH-10`, `COHORT-6`.
- **read_only_context** :
  `trader/reporting/read_models/world_cohort.py`.
- **allowed_edits** : `trader/reporting/read_models/world_patterns.py`,
  `trader/infrastructure/state_db/world_model_query.py`,
  `tests/read_models/test_world_patterns.py`.
- **sortie** : matched sets contexte/graphe, assessments, permutations, contre-exemples et
  multiplicité.
- **tests** : `uv run pytest -q tests/read_models/test_world_patterns.py
  tests/read_models/test_world_cohort_report.py`.
- **exit** : report reproductible ; outcome canonique vérifié ; aucun claim
  causal/PnL.

### Lot GRAPH-12 — runtime shadow et CLI

Hors de ce lot de lifecycle prospectif. Le matching / link / status CLI
existent déjà sous `world pattern` sans câbler le daemon, sans flag runtime
et sans lane de trading. GRAPH-12 reste le lot futur pour
`world_model_runtime.py` / `daemon.py` si une activation shadow locale est
décidée ; le défaut reste off, `shadow_only`, `NO_GO`.

## 19. Contrat d'exécution pour Grok

Chaque lot est un prompt Grok CLI natif `xhigh` autonome : fichiers autorisés,
invariants, tests rouges attendus et exit criteria. Avant édition : audit
read-only du worktree. Après édition : tests ciblés + package-layout,
`git diff --check`, puis rapport objets/events/tests/risques.

Après la commande de tests ciblés du lot, le second command obligatoire et
identique est : `uv run pytest -q tests/package_layout`.

Interdits : frontend/`desktop`, `.env`, `state`, daemon live, push, `git add -A`,
refactor hors scope ou dépendance graph lourde. Un commit logique par lot
uniquement si l'orchestrateur l'autorise avec pathspecs explicites.

## 20. Documentation après livraison et points ouverts

Après livraison, consolider référence World Model, explication ontologie et
how-to opérateur. Les voies live sont `market`, `context`, `macro_source` et
`graph`. Pas de contrat tombstone héritage.

La gate `GRAPH-CONFIG` fige avant GRAPH-5 taxonomie région/pays, sources de
mappings instrument/issuer, fenêtres/decay, profondeur/path budget et
vocabulaire de signatures ; elle est complétée avant GRAPH-7 par cohorte,
contrôles négatifs, correction de multiplicité et règle de fermeture. Les lots
référencent son hash. Aucun de ces choix n'est laissé implicitement à Grok.
