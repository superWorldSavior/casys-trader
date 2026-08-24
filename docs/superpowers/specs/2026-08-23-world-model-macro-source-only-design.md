# RFC — Flux macro source-only pour le World Model

- **Date** : 2026-08-23
- **Statut** : 💬 RFC proposée — prête pour revue ; aucune activation runtime
- **Auteurs** : Erwan + Codex
- **Portée** : collecte macro exogène, contrats point-in-time, projection bornée
  dans le World Context shadow. Ne remplace pas l'analyste macro/news Univers.
- **Autorité** : `shadow_only` / `NO_GO` ; `decision_effect=none`
- **Registre** : D19 ; cette RFC ne modifie pas l'autorité Trader
- **Dépend de** : [D19](../../decisions/registre-decisions-metier.md),
  [World Model shadow](../../explanation/architecture/world-model-shadow.md),
  [ontologie World Context](../../explanation/architecture/world-context-ontology.md)
- **Supersède** : aucun
- **RFCs sœurs** : [cohorte prospective](2026-08-23-world-model-prospective-cohort-design.md),
  [graphe et hypothèses de patterns](2026-08-23-world-model-graph-pattern-hypotheses-design.md)

## 1. Résumé et décision proposée

Créer un producteur **séparé**, `MacroWorldObservation`, qui répond uniquement
à la question suivante :

> Quelles observations macro externes étaient durablement connues pour le
> monde, une région, un pays ou une place, au cutoff `T` ?

Le flux est aveugle aux candidats, hotlists, mandats Univers, décisions Brain,
positions, portefeuille, PnL et rapports entreprise. Il collecte des faits
externes, les normalise par transformations déterministes versionnées, puis
émet une observation structurée append-only. Il n'appelle ni réseau ni LLM
dans le cycle de décision Trader.

```text
sources externes allowlistées
  -> MacroSourceFact append-only + preuve de disponibilité
  -> projection déterministe par scope
  -> MacroWorldObservation append-only + preuve de disponibilité
  -> lecture point-in-time par WorldContextReader
  -> features macro compactes dans une voie shadow distincte
```

La spec livrée de l'analyste news/macro reste inchangée. Son
`NewsMacroBrief` reçoit le `candidate_scope_id`, des familles et parfois du
contexte entreprise afin d'aider Univers. C'est une bonne sortie de politique,
mais une mauvaise entrée exogène pour le World Model. Il reste donc exclu avec
`policy_contaminated`; il n'est ni renommé, ni assaini a posteriori, ni recyclé.

## 2. Faits actuels vérifiés

1. Le contrat V2 prévoit déjà des slots compacts `macro_status`,
   `context_macro_regime`, `context_rates_regime` et `context_usd_regime`, mais
   le lecteur courant tente encore de lire `NewsMacroBrief`.
2. Le brief courant est lié au scope de candidats par conception. Sa
   régénération future peut prouver son heure de disponibilité, pas supprimer
   sa contamination de politique.
3. Les histoires brutes macro/news existantes portent des temps de collecte,
   mais pas toujours un reçu durable prouvant qu'une ligne était lisible à un
   cutoff historique. Elles ne doivent pas être rétro-jointes à des épisodes.
4. Les épisodes V1/V2, les horizons fixes 4 h/1 j, les quatre voies shadow et
   la projection NetworkX existent déjà. Cette RFC ajoute un capteur ; elle ne
   redéfinit pas le modèle.
5. GDELT peut répondre HTTP 429 et demande au moins cinq secondes entre deux
   requêtes. Une indisponibilité de source est `missing`/`stale`, jamais une
   valeur macro nulle.

Sources de contexte :
[spec analyste macro actuelle](2026-07-02-macro-analyste-news-spec.md),
[cartographie des sources macro](2026-07-02-macro-data-sources.md) et
[référence macro](../../reference/macro.md).

## 3. Objectifs et critères de succès

### 3.1 Objectifs fonctionnels

- conserver des faits macro externes avec identité, provenance, horloges et
  hash vérifiables ;
- reconstruire exactement l'observation admissible pour un scope et un cutoff ;
- projeter un vocabulaire borné, stable et explicable, sans texte brut dans le
  vecteur modèle ;
- représenter honnêtement absence, retard, péremption et couverture partielle ;
- permettre une collecte continue avant même que la voie ML soit activée ;
- rester entièrement shadow et fail-open pour le trading.

### 3.2 Critères d'acceptation du flux

Le flux est prêt pour une cohorte quand :

- deux replays sur les mêmes faits produisent le même `observation_id`, le même
  hash et le même payload canonique ;
- aucune observation ne contient de champ de la deny-list de politique ;
- chaque fait consommé a un `effective_ready_at <= cutoff_at` prouvé ;
- un crash avant le reçu produit `availability_unproven`, jamais une
  disponibilité rétroactive ;
- les erreurs, 429 et timeouts n'interrompent pas le cycle Trader ;
- la couverture et la fraîcheur sont mesurées par scope et par source ;
- le manifeste de cohorte gèle les versions du producteur et des
  transformations avant toute interprétation comparative.

Ces critères prouvent la qualité du flux, pas un gain prédictif. Le gain relève
de la [RFC cohorte](2026-08-23-world-model-prospective-cohort-design.md).

## 4. Non-buts et deny-list

Cette RFC ne cherche pas à :

- produire une recommandation, un score d'attractivité ou une direction de
  trade ;
- réentraîner le Brain, Univers, FLAIR ou MemRL ;
- reconstruire des observations historiques depuis `latest`, `current`, un
  mtime ou un `as_of` non prouvé ;
- faire de GDELT une source unique de vérité ;
- faire naviguer un LLM sur le web dans le cycle live ;
- introduire GraphRAG, un GNN ou une attribution causale ;
- modifier le frontend, le scheduler, RiskGate, le broker ou le portefeuille.

Sont interdits à tous les niveaux du payload source-only :

```text
candidate, candidate_scope, hotlist, mandate, rank, attractiveness,
brain_decision, action, intent, order, trade, position, portfolio,
risk_gate, scheduler, fill, pnl, reward, feedback, outcome,
company_brief, company_report, selected_symbol, model_rationale,
prompt, tool_trace, memory, memrl
```

La validation parcourt récursivement clés et métadonnées. Une clé interdite
fait échouer l'append de l'observation ; elle n'est jamais simplement retirée
silencieusement.

## 5. Vocabulaire et contrats versionnés

### 5.1 Scopes autorisés

`MacroScope` référence seulement :

| `kind` | Exemple | Sens |
|---|---|---|
| `world` | `market` | état global |
| `region` | `iso-un-m49:030` | regroupement géographique versionné |
| `country` | `iso-3166:TW` | pays ISO namespacé |
| `venue` | `mic:XTAI` | place de cotation namespacée |

Un `MacroSourceFact` ne cible pas `company`, `family` ou `instrument`. La propagation
vers ces entités appartient au graphe et à son hypothèse, pas au fait macro.
Hors `world:market`, `MacroScope.entity_id` utilise exactement l'identité
canonique de `WorldEntityRef` V3 ; le `MacroSourceRegistry` porte la table
versionnée qui convertit les IDs provider vers ces IDs. Aucun crosswalk
`east_asia/TW/TWSE` implicite n'est autorisé à la capture.

Les épisodes V1/V2 conservent toutefois leurs venues marché logiques actuelles
(`EU`, `TW`, `US`). Le shared kernel définit donc `WorldMarketAnchorRef`,
`WorldScopeMapping` et `WorldScopeResolution`. Le mapping versionné/hashé
résout `(market_venue, instrument)` vers zéro ou un scope venue MIC, puis ses
scopes country/region/world canoniques. `US` ou `EU` seuls ne sont jamais
convertis en une MIC par défaut : la résolution peut dépendre de l'instrument,
une absence de correspondance donne `unmapped` et plusieurs correspondances
incompatibles donnent `ambiguous`.

Les contrats ne sont pas de simples schémas JSON. Le domaine expose les types
`MacroScope`, `MacroSourceRegistry`, `MacroCollectionTarget`,
`MacroCollectionPlan`, `MacroFactKey`,
`MacroSourceFactVersionId`, `MacroSourceFact`, `MacroDimensionState`,
`MacroDerivationPolicy`, `MacroWorldObservation`, `MacroObservationEnvelope`,
`WorldScopeMapping`, `WorldScopeResolution`, `MacroCollectionRun` et l'union
`MacroCollectionEvent`. Le JSON n'est que leur sérialisation aux frontières.
`MacroObservationEnvelope` associe une
observation au `PersistedWorldRef[MacroObservationId]` et à son
`AvailabilityEvidence`; la preuve ne se perd jamais au passage du store vers
le World Context.

### 5.2 `MacroSourceFact.v1`

Un fait est une mesure ou un événement externe minimal, jamais une synthèse de
politique :

```json
{
  "schema_version": "macro_source_fact.v1",
  "fact_key": "macro_fact_key:v1:<sha256>",
  "fact_version_id": "macro_source_fact_version:v1:<sha256>",
  "fact_kind": "series_point",
  "metric_key": "policy_rate",
  "scope": {"kind": "country", "entity_id": "iso-3166:US"},
  "value": {"number": 4.25, "unit": "percent"},
  "period": "2026-08",
  "occurred_at": "2026-08-23T00:00:00Z",
  "published_at": "2026-08-23T12:30:00Z",
  "ingested_at": "2026-08-23T12:31:10Z",
  "valid_until": null,
  "source": {
    "provider_id": "official_provider",
    "adapter_version": "official_provider.v1",
    "source_record_id": "stable-provider-id",
    "source_ref": "https://source.example/record"
  },
  "supersedes_fact_version_id": null,
  "content_sha256": "<sha256>"
}
```

`fact_kind` est un vocabulaire fermé initial : `series_point` ou
`market_benchmark`. `calendar_event` et `global_event` sont réservés à une
version ultérieure : la première cohorte utilise seulement des séries
structurées. `value` est une union typée (`number` ou `category`), jamais un
dictionnaire libre. Les URLs et textes restent des preuves auditables ; ils
n'entrent pas dans le vecteur ML.

### 5.3 `MacroWorldObservation.v1`

L'observation est une projection déterministe des faits admissibles :

```json
{
  "schema_version": "macro_world_observation.v1",
  "observation_id": "macro_world_observation:v1:<sha256>",
  "scope": {"kind": "venue", "entity_id": "mic:XTAI"},
  "cutoff_at": "2026-08-23T13:00:00Z",
  "producer_version": "macro_source_only.v1",
  "transform_version": "macro_regimes.v1",
  "source_registry_version": "macro_sources.v1",
  "fact_refs": ["macro_source_fact_version:v1:<sha256>"],
  "features": {
    "macro_regime": "mixed",
    "rates_regime": "stable",
    "usd_regime": "unknown"
  },
  "coverage": {
    "status": "partial",
    "required_sources": 3,
    "fresh_sources": 2,
    "missing_source_ids": ["broad_usd_index"]
  },
  "valid_until": "2026-08-23T17:00:00Z",
  "content_sha256": "<sha256>"
}
```

Les catégories initiales sont fermées et incluent toujours `unknown`. Leurs
seuils et règles vivent dans `transform_version`; aucune constante ne peut
changer sans nouvelle version. Une couverture insuffisante donne `unknown` ou
`partial`, jamais une valeur de remplacement optimiste.

`MacroDimensionState` porte séparément chaque dimension, sa valeur, son statut
de couverture, sa méthode et les faits qui la justifient. Une observation
`partial` peut donc conserver une dimension prouvée sans inventer les autres.

### 5.4 Identité et corrections

- `MacroFactKey` = hash stable de l'identité série/événement fournisseur, du
  scope, du type de fait et de la période ;
- `MacroSourceFactVersionId` = hash content-addressed de la key, de la
  valeur/unité normalisées, de la vintage fournisseur, du schéma source et de
  la version d'adapter ;
- `observation_id` = hash du scope, cutoff, versions et liste triée des faits ;
- même version ID + même contenu = replay sans effet ;
- même version ID + contenu différent = conflit explicite ;
- une correction conserve la `MacroFactKey`, crée un nouveau
  `MacroSourceFactVersionId` et pointe
  `supersedes_fact_version_id` vers la feuille précédente ;
- une observation référence toujours des version IDs exacts ;
- l'ordre d'arrivée sur disque ne doit pas modifier le résultat canonique.

### 5.5 Cycle de vie piloté par l'agrégat

`MacroCollectionRun` est reconstruit depuis des événements typés :

```text
MacroCollectionRegistered
MacroCollectionStarted
MacroSourceCompleted | MacroSourceFailed
MacroObservationPublished
MacroCollectionCompleted
```

État dérivé :

```text
registered -> collecting -> completed
                       \--> completed_partial
                       \--> failed
```

La collecte est planifiée par `MacroCollectionPlan` : cibles immuables
`MacroCollectionTarget(scope, source_ids)` groupées depuis
`canonical_scope` du registre, liées à `registry_version` et
`content_sha256`. Aucun scope de contrôle unsourced n'est ajouté ;
`world:market` n'entre que si des sources le déclarent.
`WorldScopeMapping` reste l'autorité d'ontologie/résolution et ne schedule
pas. Le runtime itère les cibles, pas des scopes nus. Un run reçoit
exactement les `source_ids` de sa cible. Un port qui rend un fait dont le
scope ou la provenance (provider, adapter, fact_kind, metric_key, scope
canonique) diffère de la cible/entrée registre est un `MacroSourceFailed`
typé (`scope_mismatch` / `provenance_mismatch`) : le fait n'est pas persisté,
pas compté fresh, pas projeté. Le projecteur refuse indépendamment tout
ensemble mixte. Aucune observation contaminée n'est publiée.

Le runtime appelle les méthodes du run (`start`, `record_source_result`,
`publish`, `complete`) ; il ne positionne pas un statut libre. `publish()` est
refusé tant que chaque source attendue n'a pas un résultat terminal typé.
`complete()` exige un `MacroObservationEnvelope` prouvant la publication ;
sans fait admissible, le run termine `failed` sans fausse observation. Au moins
une source en échec + observation publiée donne `completed_partial`; toutes les
sources réussies donnent `completed`.

Les faits et observations sont immuables. Leur état
`availability_unproven`, `eligible`, `stale` ou `superseded` est une vue de
domaine calculée depuis reçus, cutoff et événements, jamais une mutation du
payload historique.

## 6. Horloges et règle point-in-time

| Horloge | Définition | Usage modèle |
|---|---|---|
| `occurred_at` | instant du phénomène mesuré | descriptif |
| `published_at` | instant explicite de publication par la source | audit |
| `ingested_at` | fin d'acquisition/validation locale | audit |
| `ready_at` | estampille du reçu après fsync de l'histoire | borne candidate |
| `first_seen_at` | première lecture effective du reçu par le consumer | anti-rétroactivité |
| `effective_ready_at` | `max(ready_at, first_seen_at)` | éligibilité |
| `cutoff_at` | clôture de la barre de l'épisode | sélection des faits |
| `valid_until` | expiration déterministe de la vintage fournisseur | fraîcheur |

`valid_until` d'un fait est `published_at` plus le TTL opérateur figé. Une
re-observation locale (`observed_at`, `ingested_at`, horloge runtime) ne mute
jamais un fait.

Un fait ou une observation est admissible seulement si :

```text
effective_ready_at <= cutoff_at
and (valid_until is null or cutoff_at < valid_until)
and source/adapter/transform versions are admitted by the cohort manifest
```

`published_at <= cutoff_at` ne suffit pas. `as_of`, mtime, cache `latest` et
date incluse dans un nom de fichier ne prouvent jamais la disponibilité.

Séquence d'écriture canonique : append JSONL → flush → fsync → création du
reçu avec horloge injectée → append reçu → fsync. Le consumer réutilise le
garde-fou `effective_ready_at` déjà défini pour le World Context.

Le même protocole vaut en SQLite : transaction durable du sujet/event, puis
transaction séparée du reçu dans la table append-only commune
`world_availability_receipts`. Le payload initial n'embarque donc jamais une
preuve prétendument créée après son propre commit. Un repository ne retourne
un `PersistedWorldRef` éligible qu'après le commit du reçu ; un crash entre les
deux laisse le sujet durable mais `availability_unproven`, et un retry
idempotent peut seulement compléter le reçu manquant. Après restart, le reader
rejoint sujet + reçu et applique une nouvelle borne conservatrice
`first_seen_at`.

### 6.1 Shared kernel de disponibilité

Avant tout nouvel adapter, généraliser le helper dict actuel en types de
domaine communs dans `trader/domain/world_availability.py` :

```text
WorldAvailabilitySubjectRef
WorldAvailabilityReceipt
AvailabilityEvidence
PersistedWorldRef[T]
PointInTimeEligibilityPolicy
```

`WorldAvailabilityReceipt` contient schema/id du sujet, hash, scope et chemin
canonique (ou identité table/row pour SQLite) ; son `ready_at` est attribué
uniquement par l'adapter de persistance
après fsync. Aucun constructeur/use case public n'accepte un `ready_at` fourni
par le runtime. Le reader enrichit le reçu par `first_seen_at` et produit
`AvailabilityEvidence(effective_ready_at=max(...))`. Le domaine décide
l'éligibilité via `PointInTimeEligibilityPolicy`; l'infrastructure ne décide
pas qu'un fait est causalement admissible.

Ce shared kernel est également utilisé par `WorldCohortStarted`, les événements
d'ontologie, `WorldGraphSnapshot` et les occurrences de pattern. Le schéma
actuel limité à `artifact_ref.brief_id` est versionné/généralisé ; les anciens
reçus restent lisibles par un adapter de compatibilité.

Pour les objets de `world_model.db`, le schéma partagé minimal est : subject
kind/ID, content hash, receipt ID, `ready_at`, scope, storage locator et receipt
hash. Une contrainte unique `(subject_kind, subject_id, content_sha256)` rend
le retry idempotent ; des triggers interdisent `UPDATE`/`DELETE`. Cette table
est l'autorité des reçus SQLite pour cohorte, ontologie, snapshots et patterns.

### 6.2 Shared kernel de scopes

`WorldScopeMapping.v1` est immuable et profondément hashé. Chaque entrée porte
une ancre marché (`market_venue` + sélecteur instrument explicite), les refs
canoniques venue/country/region/world, ses preuves provider et sa version de
taxonomie. Deux entrées applicables à la même ancre avec des sorties différentes
sont un conflit de domaine. `WorldScopeResolution` persiste mapping ID/hash,
ancre résolue, scopes ordonnés et statut `resolved | unmapped | ambiguous`.

Le domaine effectue cette résolution pure ; l'adapter charge le fichier
configuré. Aucune heuristique `TW -> XTAI`, `US -> XNYS` ou `EU -> ...` ne vit
dans le runtime/reader. Une nouvelle cotation ou correction crée une nouvelle
version du mapping et donc un nouveau manifeste de cohorte.

## 7. Owners DDD et règles d'import

| Couche | Owner proposé | Responsabilité |
|---|---|---|
| Shared kernel domaine | `trader/domain/world_availability.py` | receipt/evidence/PIT policy typés |
| Shared kernel scopes | `trader/domain/world_scope.py` | ancre marché, mapping et résolution canonique |
| Domaine | `trader/domain/world_macro.py` | contrats immuables, vocabulaires, hashes, temporalité, deny-list |
| Application | `macro_pipeline.py`, `macro_ports.py` | ports consumer-owned, collecte orchestrée, projection déterministe |
| Infrastructure sources | `trader/infrastructure/market_sources/world_macro/` | adapters provider, rate limiting, parsing |
| Infrastructure état | `trader/infrastructure/state_db/world_macro_store.py` | histoires + reçus append-only, lookup PIT |
| Runtime | `trader/runtime/world_macro_runtime.py` | worker single-flight, budgets, logs fail-open |
| Reporting | `trader/reporting/read_models/world_macro_status.py` | couverture/fraîcheur, aucune mutation |
| Interface | `trader/interfaces/cli/world_model.py` | façade CLI mince vers reporting |

Règles : domaine stdlib-only ; application n'importe ni runtime, ni
infrastructure, ni reporting ; adapters implémentent des ports consumer-owned ;
CLI sans SQL ; runtime reste composition root. `NewsMacroBrief` et son store
ne sont pas importés par le nouveau domaine.

Le découpage ne force pas un déplacement massif de `world_context.py`. Si un
split devient utile, un lot de refactor dédié conserve une façade de
compatibilité et passe les tests de layout avant toute feature.

### 7.1 Ports consumer-owned

```python
class MacroSourcePort(Protocol):
    def read_facts(
        self, scope: MacroScope, observed_at: datetime
    ) -> tuple[MacroSourceFact, ...]: ...

class MacroHistory(Protocol):
    def append_fact(
        self, fact: MacroSourceFact
    ) -> PersistedWorldRef[MacroSourceFactVersionId]: ...
    def append_observation(
        self, observation: MacroWorldObservation
    ) -> MacroObservationEnvelope: ...

class WorldMacroObservationReader(Protocol):
    def list_candidates_available_through(
        self, scope: MacroScope, cutoff_at: datetime
    ) -> tuple[MacroObservationEnvelope, ...]: ...

class MacroCollectionLedger(Protocol):
    def append_event(
        self, event: MacroCollectionEvent
    ) -> PersistedWorldRef[MacroCollectionEventId]: ...
    def load(self, run_id: MacroCollectionRunId) -> tuple[MacroCollectionEvent, ...]: ...
```

Ces ports vivent côté application. Les signatures n'acceptent ni dictionnaire
arbitraire, ni `ready_at` caller-controlled, ni objet Univers/Brain/company.
`MacroDerivationPolicy` est un service de domaine pur injecté dans le use case ;
le runtime ne choisit pas les seuils. Le reader retourne des candidats et
preuves, jamais un `latest` déjà déclaré causal par l'infrastructure ; le use
case applique `PointInTimeEligibilityPolicy`, puis choisit déterministiquement
la dernière observation admissible.

## 8. Stockage et provenance

Chemins proposés :

```text
state/world_macro/facts/YYYY-MM-DD.jsonl
state/world_macro/facts/availability_receipts/YYYY-MM-DD.jsonl
state/world_macro/observations/YYYY-MM-DD.jsonl
state/world_macro/observations/availability_receipts/YYYY-MM-DD.jsonl
state/world_macro/runs/events/YYYY-MM-DD.jsonl
state/world_macro/runs/availability_receipts/YYYY-MM-DD.jsonl
state/world_macro/status.json              # projection reconstructible
```

Les trois histoires sont canoniques et append-only. `status.json` peut être
écrasé atomiquement car il est reconstructible. Pas de purge automatique tant
que la politique de rétention de la cohorte n'est pas décidée. Un index SQLite
futur est une projection, pas une nouvelle vérité.

Les lignes historiques existantes sans receipt peuvent alimenter une nouvelle
observation uniquement après réingestion/validation prospective : leur
disponibilité commence alors, jamais à leur ancienne `period` ou `as_of`. Elles
ne permettent aucun backfill de cohorte.

Chaque observation conserve les `fact_refs`; chaque fait conserve l'identité
de l'adapter et la référence source. Les hashes sont recalculés à la lecture.
Une preuve invalide est visible en reporting mais exclue du modèle.

## 9. Sources, cadence et budgets

Le registre `macro_sources.v1` est versionné et configuré. Il commence par :

- séries officielles déjà cartographiées dans le projet ;
- benchmarks de marché explicitement désignés comme macro, distincts de
  l'OHLCV instrument V1 ;
- aucune news/GDELT dans `macro_world_observation.v1`. Le capteur événementiel
  aura un contrat séparé après preuve de couverture et de débit.

Le registre initial part des séries structurées effectivement présentes
(taux Fed/BCE, CPI/HICP, chômage disponible, Brent et or), mais encode leurs
gaps : pas encore de série USD large, couverture croissance/Taïwan incomplète,
chômage US potentiellement périmé et bruit de rollover des futures. Une
dimension non couverte reste `unknown`.

Les collecteurs historiques dédupliquent aujourd'hui par seule période. Le
contrat cible déduplique par `(series_id, period, value, unit)` : une valeur
corrigée sur la même période crée une nouvelle vintage et `supersedes` la
précédente. Aucun Grok ne doit décider de jeter une révision parce que la date
de période est identique.

Contraintes runtime :

- aucun fetch dans `run_cycle` ni dans le worker World Model qui capture une
  barre ;
- un seul worker de collecte, file coalescente et budget par provider ;
- le worker itère les `MacroCollectionTarget` du plan ; US/Europe/world
  n'invoquent que leurs sources déclarées, jamais tout le registre ;
- pour une future version événementielle, limiter GDELT à au plus une requête
  toutes les cinq secondes, honorer `Retry-After`, appliquer backoff avec
  jitter et cache local ;
- dédupliquer avant append par identité fournisseur ;
- timeout ou 429 → source `missing` avec motif, sans boucle agressive ;
- tests adapters sur fixtures, jamais sur le réseau public ;
- aucun LLM en V1 du producteur. Une synthèse LLM source-only éventuelle sera
  une RFC/arm distincte, avec prompt et modèle gelés.

## 10. Intégration au World Context

1. Étendre le vocabulaire d'artefacts avec `macro_world_observation` sans
   relâcher la deny-list.
2. Consommer le port typé `WorldMacroObservationReader` ci-dessus depuis
   `WorldContextReader` ou un composite application-owned ; conserver
   `MacroObservationEnvelope` jusqu'à la construction de l'artefact.
3. Résoudre d'abord l'ancre `(market_venue, instrument)` avec le
   `WorldScopeMapping` dont version/hash sont gelés dans le manifeste, puis
   sélectionner l'observation éligible du scope canonique exact. Une résolution
   `unmapped` ou `ambiguous` donne macro missing tout en conservant ce statut
   exact. Chaque `MacroWorldObservation` ne porte que des faits du même scope
   canonique que sa cible de collecte. Country, region et world restent des
   observations distinctes ; le producteur ne tamponne jamais un scope de run
   sur des faits étrangers. Aucun fallback implicite du reader ne peut
   changer silencieusement la population de sources.
4. Projeter uniquement les catégories allowlistées. Raw facts, titres, URLs et
   texte sont hors feature vector.
5. Garder `market_ohlcv_context.v2` seulement si noms et sens des features
   existantes ne changent pas. La voie reçoit malgré tout une nouvelle identité
   `context.v2.macro_source.v1` et un départ de cohorte propre. Toute nouvelle
   feature ou nouvelle sémantique impose un bump de feature contract.
6. Ne jamais entraîner la voie macro sur des épisodes antérieurs au manifeste
   de cohorte, même s'ils peuvent être relus techniquement.

## 11. Rollout et gates

| Phase | Effet | Gate de sortie |
|---|---|---|
| M0 — contrats | aucun runtime | invariants/hashes/deny-list testés |
| M1 — collecte locale | écrit seulement `state/world_macro/` | replay, crash et 429 testés |
| M2 — observation shadow | reporting de couverture | cutoff et contamination 100 % conformes |
| M3 — attache V2 opt-in | nouveaux épisodes/lanes shadow | manifeste de cohorte armé |
| M4 — étude | évaluation appariée | protocole de la RFC cohorte atteint |

Flag proposé, défaut off et lu au boot :

```text
CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED=0
```

L'implémentation de la RFC ne modifie pas `.env`, ne redémarre pas le daemon et
n'active pas ce flag. Même activé ultérieurement, `authority=shadow_only`,
`decision_effect=none` et `NO_GO` restent invariants.

Stop immédiat / cohorte invalidée si : contamination de politique, horloge
naïve, receipt manquant accepté, changement de version non déclaré, payload
mutable, collision silencieuse, I/O réseau dans le cycle ou dépendance d'une
décision au résultat macro.

## 12. Tests obligatoires

### Domaine

- round-trip et hash déterministes ;
- immutabilité profonde ;
- tous les vocabulaires et valeurs `unknown` ;
- deny-list récursive, y compris clés camelCase et imbriquées ;
- corrections par `supersedes`, conflit same-ID/different-content ;
- frontières de validité exactes (`< valid_until`).

### Store et causalité

- append idempotent, fsync avant reçu, receipt tamperé rejeté ;
- crash entre histoire et reçu ;
- reçu découvert pendant un scan après cutoff ;
- restart conservateur, aucun recours au mtime ;
- sélection PIT de la venue exacte et composition globale explicite ;
- ordre d'append aléatoire donnant la même observation.

### Adapters/runtime

- fixtures parse/reject ; unités, timezone et valeurs manquantes ;
- 429/timeout/backoff sans retry storm ;
- worker single-flight/coalescent et shutdown borné ;
- zéro appel adapter depuis `run_cycle` ;
- erreur du flux sans effet sur la décision Trader.

### Architecture/reporting

- tests de layout DDD et interdictions d'import ;
- CLI/read model lecture seule sur état absent/corrompu/partiel ;
- statut séparant collecte, observation, attache et cohorte ;
- invariants `shadow_only`, `NO_GO`, `decision_effect=none`.

## 13. Lots Grok CLI bornés

Chaque lot commence par un test rouge ciblé, se termine par les tests du lot +
tests de layout, et ne mélange pas refactor et feature non nécessaires.

Interdits pour **tous** les lots : `desktop/**`, `.env`, `state/**`,
`docs/README.md`, `docs/reference/world-model.md` tant que son autre scope est
sale, restart/kill du daemon, push, migration de données live et fichier non
listé dans `allowed_edits`.

### Lot MACRO-0 — shared kernel de disponibilité

- **depends_on** : aucun.
- **read_only_context** : `trader/domain/world_context.py`,
  `trader/infrastructure/state_db/world_context_reader.py`.
- **allowed_edits** : `trader/domain/world_availability.py`,
  `trader/domain/world_scope.py`,
  `trader/infrastructure/state_db/availability_receipt.py`,
  `tests/domain/test_world_availability.py`,
  `tests/domain/test_world_scope.py`,
  `tests/state_db/test_availability_receipt.py`,
  `tests/package_layout/test_world_model_layout.py`.
- **types/events livrés** : receipt, evidence, persisted ref, policy PIT,
  ancre/mapping/résolution de scopes ; compatibilité des reçus historiques.
- **tests** : `uv run pytest -q tests/domain/test_world_availability.py
  tests/domain/test_world_scope.py
  tests/state_db/test_availability_receipt.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : seul le store attribue `ready_at`; les callers ne peuvent pas le
  forger ; comportement V2 existant inchangé.

### Lot MACRO-1 — domaine macro et cycle de vie

- **depends_on** : `MACRO-0`.
- **read_only_context** : présente RFC, `trader/domain/world_episode.py`.
- **allowed_edits** : `trader/domain/world_macro.py`,
  `tests/domain/test_world_macro.py`.
- **types/events livrés** : tous les types §5, `MacroCollectionRun`, union
  complète d'événements et résultat terminal typé.
- **tests** : `uv run pytest -q tests/domain/test_world_macro.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : corrections/vintages, deny-list, hash, transitions et invariants
  publish/complete testés ; stdlib-only.

### Lot MACRO-2 — ports et use cases application

- **depends_on** : `MACRO-1`.
- **read_only_context** : `trader/application/world_model/capture.py`,
  `trader/application/world_model/context_capture.py`.
- **allowed_edits** : `trader/application/world_model/macro_pipeline.py`,
  `trader/application/world_model/macro_ports.py`,
  `tests/application/test_world_macro_pipeline.py`.
- **sortie** : ports §7.1, orchestration run/facts/observation bornée par
  cible typée, et politique de dérivation injectée.
- **tests** : `uv run pytest -q tests/application/test_world_macro_pipeline.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : aucune importation infrastructure/runtime/reporting ; même input +
  versions = même observation ; missingness honnête.

### Lot MACRO-3 — stores append-only

- **depends_on** : `MACRO-0`, `MACRO-1`, `MACRO-2`.
- **read_only_context** :
  `trader/infrastructure/state_db/_jsonl_store.py`,
  `trader/infrastructure/state_db/availability_receipt.py`.
- **allowed_edits** : `trader/infrastructure/state_db/world_macro_store.py`,
  `tests/state_db/test_world_macro_store.py`.
- **sortie** : histoires facts/observations/run-events + reçus, lookup PIT,
  conflits et replay.
- **tests** : `uv run pytest -q tests/state_db/test_world_macro_store.py
  tests/state_db/test_availability_receipt.py`.
- **exit** : crash/restart/tamper/first-seen testés ; aucune mutation historique.

### Gate MACRO-CONFIG — décision opérateur, non délégable

Avant `MACRO-4`, un opérateur doit valider et committer
`config/world_macro_sources.yaml` et
`config/world_macro_derivation_policy.yaml`, ainsi que le mapping partagé
`config/world_scope_mapping.yaml`. Ces artefacts versionnés et hashés
figent provider/series IDs, scopes, cadence, TTL, seuils de régime, budgets et
rétention. Grok peut valider leur schéma mais ne choisit aucune valeur. Tant
qu'un champ requis ou un hash manque, `MACRO-4` est `blocked_until` et le
workflow termine `NO_GO` sans édition.

### Lot MACRO-4 — adapters et registre de sources

- **depends_on** : `MACRO-2`, `MACRO-3`, `MACRO-CONFIG`.
- **read_only_context** :
  `docs/superpowers/specs/2026-07-02-macro-data-sources.md`,
  `config/world_macro_sources.yaml`,
  `config/world_macro_derivation_policy.yaml`,
  `config/world_scope_mapping.yaml`,
  `trader/infrastructure/market_sources/macro_series.py`,
  `trader/infrastructure/market_sources/commodity_prices.py`.
- **allowed_edits** :
  `trader/infrastructure/market_sources/world_macro/__init__.py`,
  `trader/infrastructure/market_sources/world_macro/series.py`,
  `trader/infrastructure/market_sources/macro_series.py`,
  `trader/infrastructure/market_sources/commodity_prices.py`,
  `tests/infrastructure/test_world_macro_series_adapter.py`,
  `tests/test_macro_series.py`, `tests/test_commodity_prices.py`.
- **sortie** : registry versionné, adapters séries/commodités, vintages et
  rate/backoff ; aucune source événementielle V1.
- **tests** : `uv run pytest -q
  tests/infrastructure/test_world_macro_series_adapter.py
  tests/test_macro_series.py tests/test_commodity_prices.py`.
- **exit** : fixtures seulement, zéro réseau en test, corrections de période
  conservées, gaps source explicites.

### Lot MACRO-5 — intégration World Context

- **depends_on** : `MACRO-3`, `MACRO-4`, `COHORT-4` ; le pilote non-macro
  livre d'abord les contracts/encoders partagés, puis cette extension ajoute la
  projection macro sans collision.
- **read_only_context** : `trader/domain/world_episode.py`,
  `trader/application/world_model/encoding.py`.
- **allowed_edits** : `trader/domain/world_context.py`,
  `trader/application/world_model/context_capture.py`,
  `trader/application/world_model/world_scope_resolver.py`,
  `trader/infrastructure/state_db/world_context_reader.py`,
  `tests/domain/test_world_context.py`,
  `tests/application/test_world_context_capture.py`,
  `tests/application/test_world_scope_resolution.py`,
  `tests/state_db/test_world_context_reader.py`,
  `tests/application/test_world_model_feature_contracts.py`.
- **sortie** : résolution d'ancre prouvée, artifact source-only, preuve exacte,
  extraction dimensionnelle partielle, suppression de la source modèle
  `NewsMacroBrief`.
- **tests** : `uv run pytest -q tests/domain/test_world_context.py
  tests/application/test_world_context_capture.py
  tests/application/test_world_scope_resolution.py
  tests/state_db/test_world_context_reader.py
  tests/application/test_world_model_feature_contracts.py`.
- **exit** : fingerprint V1 inchangé ; aucun fallback venue/scope/macro
  implicite ; mapping ID/hash persistant ; ancien pipeline Univers intact.

### Lot MACRO-6 — runtime shadow opt-in

- **depends_on** : `MACRO-2`, `MACRO-3`, `MACRO-5`, `COHORT-5` ; le runtime
  cohorte du pilote est livré avant l'extension du composition root macro.
- **read_only_context** : `trader/runtime/world_model_runtime.py`.
- **allowed_edits** : `trader/runtime/world_macro_runtime.py`,
  `trader/runtime/daemon.py`, `tests/runtime/test_world_macro_runtime.py`,
  `tests/runtime/test_daemon_world_model.py`.
- **sortie** : worker coalescent/fail-open, flag défaut off, identité de lane
  macro distincte, aucune I/O dans le cycle.
- **tests** : `uv run pytest -q tests/runtime/test_world_macro_runtime.py
  tests/runtime/test_daemon_world_model.py`.
- **exit** : producer-on/V2-off possible ; panne sans effet Trader ; le lot ne
  redémarre aucun process.

### Lot MACRO-7 — reporting et CLI read-only

- **depends_on** : `MACRO-3`, `MACRO-6`, `COHORT-7` ; la CLI cohorte du pilote
  est livrée avant l'ajout des vues macro.
- **read_only_context** : `trader/reporting/read_models/world_status.py`,
  `trader/interfaces/cli/world_model.py`.
- **allowed_edits** : `trader/reporting/read_models/world_macro_status.py`,
  `trader/reporting/read_models/world_status.py`,
  `trader/interfaces/cli/world_model.py`, `trader/runtime/cli.py`,
  `tests/read_models/test_world_macro_status.py`,
  `tests/test_cli_world_model.py`.
- **sortie** : collecte/couverture/fraîcheur/gaps séparés de l'étude ML.
- **tests** : `uv run pytest -q tests/read_models/test_world_macro_status.py
  tests/test_cli_world_model.py tests/package_layout/test_world_model_layout.py`.
- **exit** : statut DB/état absent strictement read-only ; claims shadow bornés.

## 14. Contrat d'exécution pour Grok

Pour transformer un lot en prompt Grok CLI natif `xhigh` :

1. fournir uniquement la présente RFC, le lot choisi et les fichiers cités ;
2. demander un audit read-only initial du worktree et des imports ;
3. interdire `desktop/`, `docs/README.md`, le frontend, `.env`, `state/`, le
   restart daemon, le push et tout fichier non listé ;
4. exiger TDD, `git diff --check`, tests ciblés et tests package-layout ;
5. demander un compte rendu `fichiers / invariants / tests / risques restants` ;
6. un lot = un commit logique seulement si l'orchestrateur l'autorise, avec
   pathspecs explicites ; jamais `git add -A`.

Un lot ne peut pas anticiper le suivant en créant des stubs non testés. Un
écart nécessaire à la RFC remonte comme point de décision avant modification.
Après la commande de tests ciblés propre au lot, le second command obligatoire
et identique pour tous les lots est :
`uv run pytest -q tests/package_layout`.

## 15. Documentation après livraison

Après implémentation — pas au stade RFC — consolider :

- `docs/reference/world-model.md` : contrat macro, versions et statut live ;
- `docs/explanation/architecture/world-context-ontology.md` : flux réel ;
- `docs/how-to/operate-world-model-shadow.md` : commandes et flags vérifiés ;
- D19 : uniquement si son résumé d'implémentation doit être complété.

## 16. Décisions à figer par `MACRO-CONFIG` avant MACRO-4

1. liste exacte des providers initiaux et cadence par provider ;
2. taxonomie géographique `region/country/venue` et version de mapping ;
3. seuils déterministes de `macro_regime`, `rates_regime`, `usd_regime` ;
4. durée de validité par type de fait ;
5. politique de rétention des histoires brutes.

Ces choix sont gelés dans les trois artefacts de `MACRO-CONFIG`, puis référencés
par hash dans le registre et les versions de transformation. Ils ne remettent
pas en cause la frontière source-only et ne peuvent pas être inventés par un
lot d'implémentation.
