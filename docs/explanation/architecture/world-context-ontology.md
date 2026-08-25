# World Context / ontologie temporelle — contexte et ombre graphe

> **Type** : Explanation (Diataxis). Capacité **shadow-only** et **fail-open**
> pour le trading, **fail-closed** pour la causalité et l'apprentissage
> inter-voies. Elle n'a aucune autorité de trading et n'est jamais promue
> automatiquement. Le contrat marché reste
> [world-model-shadow](world-model-shadow.md).

## Question à laquelle le graphe répond

Le graphe d'ontologie dit, pour un cutoff marché **prouvé** :

> « Qu'est-ce qui était connu, à propos de quelle entité, depuis quelle
> source, au plus tard à ce cutoff ? »

Il relie un instrument à sa place, éventuellement une famille déjà gelée dans
l'observation marché, éventuellement un émetteur *vérifié par identifiant
externe*, et aux capteurs (macro source-only, company) dont la disponibilité
est prouvée par un reçu. C'est de la **provenance** et de la **traversée**.
Ce n'est pas une découverte causale, pas un graphe de message passing, et
pas un agrégat familial. Producteur, scope d'origine, distance d'ancestry
et lignée de faits se reconstruisent depuis les `source_refs` typés
(`MacroObservationProvenance`) ; on n'invente pas un MIC, un scope ou une
distance.

## Question à laquelle il ne répond pas

Il ne dit pas qu'une association *cause* un mouvement de marché. Il n'y a
pas d'arête générique `CAUSES` (ni `HYPOTHESIZED_INFLUENCE` persistée comme
fait). Il ne dit pas si un trade était bon, et il ne produit ni PnL de
portefeuille ni recommandation. Une ablation appariée ne mesure qu'un delta
prédictif (log-loss, Brier), sans claim économique ni claim causal.

La `venue` marché/contexte est un scope marché logique (`EU`, `TW`, `US`). Le mapping
`world_scope_mapping.v1` est le **contrat de schéma** ; le `content_sha256` est
la génération. Il résout `(market_venue, instrument)` vers les scopes
canoniques (`mic`, pays, région). Aucun fallback `TW -> XTAI` ou `US -> XNYS`
silencieux. Les cotations US distinguent XNYS et XNAS via les métadonnées
provider. Un scope `unmapped` ou `ambiguous` produit une missingness
canonique, pas une invention : snapshot graphe `world_graph_snapshot.v1` sans
racine ni membres (`root_entity` JSON `null` est un élargissement
compatible de la missingness, pas un bump de schéma). L'ontologie
structurelle commitée reste la famille `market_ontology.v1`. L'instance
est `market_ontology:v1:<mapping_sha256>`. Un store vide publie la
génération courante. Un store déjà publié, même famille, hash différent,
append une successeure et supersede l'active (PIT inchangé). Une autre
identité de schéma (`market_ontology.v2`) est un conflit.

## Horloges point-in-time

Quatre horloges distinctes. Les fusionner recrée le bug « reçu apparu après
le cutoff mais daté avant ».

| Horloge | Sens | Qui l'écrit |
|---|---|---|
| `cutoff_at` | « connu au plus tard à T » pour une ancre / une observation | cutoff barre (contexte) ou cutoff de collecte (macro) |
| `ready_at` | durabilité du reçu, **après** fsync du sujet | le store, jamais l'appelant |
| `first_seen_at` | première lecture effective de ce reçu par le consumer | reader, horloge injectée **après** découverte |
| `effective_ready_at` | `max(ready_at, first_seen_at)` | `AvailabilityEvidence` |

Règle de domaine : éligible seulement si `effective_ready_at <= cutoff_at`
et `cutoff_at < valid_until` (sinon `stale`). Reçu absent, non attesté ou
lu après le cutoff → `availability_unproven`, collapsed en missing modèle
(`no_proven_artifact_at_cutoff`). Pas de mtime, pas d'`as_of` sémantique
comme preuve, pas de mutation des sidecars.

Séquence d'append (histoire brute JSONL ou SQLite) :

1. écrire le sujet canonique ;
2. `flush` + `fsync` ;
3. **alors seulement** estampiller `ready_at` depuis une horloge injectée ;
4. append + `fsync` du reçu sidecar / table de reçus.

Un crash avant le reçu laisse une ligne brute visible mais
`availability_unproven`. Un restart est volontairement conservateur pour
les anciens cutoffs, sauf si un épisode contexte/graphe canonique existe déjà.

Cutoff de voie contexte/graphe = horloge d'**achèvement de barre** :

- `bar_close` → timestamp d'ancre ;
- `bar_start` → ancre + intervalle parsé ;
- inconnu / inparsable → pas d'attache (fail closed, jamais de guess).

Il doit rester `<= observation.available_at`. On ne réécrit pas le
`available_at` voie marché (heure de fetch).

Pour une cohorte : `anchor_end_at` du slot doit être **strictement après**
l'`effective_ready_at` du `WorldCohortStarted` prouvé. Activation pilote :
`planned_start_not_before` = `boot_event_time` du premier boot,
`collection_stop_at` = start + 7 j. Pas une date ISO pré-expirée.

## Cap produit : des cas sémantiques aux chaînes évaluées

Le cap n'est pas de remplacer les mémoires FLAIR / MemRL par un modèle
monolithique. Il est de les compléter par une représentation temporelle du
monde afin qu'un rappel puisse référencer une chaîne structurée :

```text
observation macro → région/pays ← place ← instrument cible
                                      ├→ famille
                                      └→ entreprise
instrument cible → outcome à horizon fixe
```

Une telle chaîne, si elle existe, est un objet versionné
(`PatternHypothesis` → `PatternOccurrence` → `PatternOutcomeLink`
prospectif). Le graphe porte les entités, relations et provenances ; le
baseline / GRU teste la valeur prédictive d'un vecteur borné ; FLAIR et
MemRL gardent leurs rôles documentés dans
[`agent-knowledge-architecture.md`](../../reference/agent-knowledge-architecture.md).

Avant contrôles négatifs, permutations et support prospectif suffisant,
une chaîne reste une **hypothèse prédictive**, jamais une causalité
établie. Le vocabulaire public reste `predictive_hypothesis` /
`predictive_association_observed` / `not_supported` / `invalidated`. Le
mot `causal` ne qualifie jamais un résultat live.

## Ce qui est collecté aujourd'hui

| Capteur | Source | Statut actuel |
|---|---|---|
| Marché OHLCV | capture marché déjà gelée | contrôle, toujours émis si ancre valide |
| Macro source-only | `MacroWorldObservation` (`world_macro_source.v1`) | éligible si envelope + reçu PIT **et** producteur admis live ; worker opt-in / OR YAML. Store frais uniquement. |
| Macro Univers | `NewsMacroBrief` | **exclu** comme source modèle World Context (`policy_contaminated`) |
| Événements GDELT | `state/gdelt/` | **exclu** du registre macro source-only |
| Company micro | `CompanyIntelligenceBrief` | éligible seulement si sidecar-prouvé **et** action-free |
| Famille | `categorical_features.asset_family` gelé dans le marché | topologie descriptive `MEMBER_OF_FAMILY`, pas une lookup catalogue courante |

`WorldContextReader.lookup_macro` consomme uniquement les envelopes
source-only. Le constructeur peut encore recevoir un `news_store` pour
compatibilité : ce n'est **pas** une source modèle. GDELT continue d'alimenter
l'analyste Univers ; ça ne le réintroduit pas dans le World Context.

Allowlist macro actuelle : DBnomics (taux, CPI, chômage) et Yahoo Finance
(brent, or), registre `world_macro_sources.v1` / adapters
`world_dbnomics_series.v1` et `world_yahoo_commodity.v1`. Exclus aussi : FRED,
ecbdata, OECD. Gaps déclarés (USD large, taux/CPI Taiwan, séries de
croissance) restent des missingness honnêtes, jamais une valeur nulle
inventée. `valid_until` = `published_at` + TTL figé ; `source_ref` est
la ressource canonique, distincte des query de transport
(`observations=1`, `metadata=0`). Un premier cutoff peut stager le
fait (reçu après cutoff, run `no_admissible_observation`) ; un cutoff
ultérieur peut le publier. Une série amont déjà hors TTL reste
`stale`. Au boot, la feuille `supersedes` est hydratée depuis les
faits reçus-prouvés compatibles. Voir
[RFC macro §17](../../superpowers/specs/2026-08-23-world-model-macro-source-only-design.md).

Deny-list de politique (parcours récursif) : candidate, hotlist, mandat,
Brain, ordre, fill, PnL, company_brief, mémoire, etc. Une clé interdite
fait échouer l'append ; elle n'est pas silencieusement retirée.

Les RFCs restent la trace de conception :

- [flux macro source-only](../../superpowers/specs/2026-08-23-world-model-macro-source-only-design.md) ;
- [cohorte prospective appariée](../../superpowers/specs/2026-08-23-world-model-prospective-cohort-design.md) ;
- [graphe temporel et hypothèses de patterns](../../superpowers/specs/2026-08-23-world-model-graph-pattern-hypotheses-design.md).

Le comportement livré (pilote `pipeline_pilot`, encore `NO_GO`) est dans
la [note de statut](../../decisions/2026-08-24-world-model-shadow-pilot.md).

## Records typés + projection NetworkX

Les records (`EntityRef` / `WorldEntityRef`, `TopologyEdge` /
`WorldRelation`, `KnowledgeArtifact`, `WorldContextSnapshot` /
`WorldGraphSnapshot`) sont la vérité canonique : immuables, hash
déterministe, stdlib only dans le domaine.

`trader.infrastructure.graph` construit un `MultiDiGraph` **frais** pour
la traversée et la validation, clé par digest d'arête afin de conserver
des arêtes temporelles / provenance parallèles. Muter ce graphe ne change
pas les records. **Aucun objet NetworkX n'est persisté**
(`world_graph_store` l'interdit). La topologie n'est pas encodée dans le
Markov/GRU marché ; la voie graphe encode un vecteur borné (`topology_status_only`
vs `graph_content`), pas un GNN.

Relations structurelles : `PART_OF_WORLD`, `LOCATED_IN`, `TRADED_ON`,
`ISSUED_BY`, `MEMBER_OF_FAMILY`. Connaissance : `ABOUT`, `OBSERVES`,
`DERIVED_FROM`, `SUPERSEDES`, `USES`. Interdites : `CAUSES`, `CAUSE`,
`CAUSED_BY`, `CAUSAL`, `HYPOTHESIZED_INFLUENCE`.

Budgets de traversée (YAML + overlay status) : profondeur 4, 32 chemins
par racine, cycles interdits. Un snapshot `missing` / `stale` / budget
dépassé s'encode en missingness ; il ne droppe pas le marché.

L'émetteur : LEI, CIK ou provider `issuer:` approuvé. Un ISIN identifie
l'instrument, jamais l'entreprise. Un `issuer_name` n'est pas un
identifiant.

`config/world_graph.yaml` conserve `cohort_id: null` (C1 n'a pas de
voies graphe). `compose_local_graph_lanes(..., study_cohort_id=...)`
injecte l'id de la cohorte `graph` matérialisée au boot. Le YAML n'est
pas une autorité de cohorte.

Un graphe **câblé** au boot n'écrit rien (`writes=none_until_due_cycle`).
Un cycle dû dont l'ancre est `unmapped` ou `ambiguous` écrit le marché et un
compagnon graphe missing/status-only, sans racine d'entité monde ni topologie
inventée. Un cycle idle ou un replay exact restent un no-op.

## Marché, contexte et graphe comme capacités distinctes

| Voie | Contrat | Identité typique |
|---|---|---|
| Markov / GRU marché | `world_feature.market.v1` | baseline / challenger historiques |
| Markov / GRU contexte | `world_feature.context.v1` | `context.v1` si le store macro est câblé |
| Lanes C1 | masques market / status / company / macro / joint | GRU froid `sequence_length=4` |
| Markov / GRU graphe | `world_feature.graph.v1` | `topology_status_only` vs `graph_content` |

Le marché reste immuable : mêmes payloads, mêmes `episode_id` marché. Le
contexte dérive l'observation du **même slot de barre**, y attache un snapshot
au cutoff d'achèvement de barre, identité distincte incluant le digest du
snapshot. Le graphe attache **un** compagnon par slot marché, sans élargir
marché ni contexte.

La persistance contexte/graphe est canonical-first-write. Un replay exact reste
un no-op. Chaque entrée publique baseline / GRU enforce `accepts_episode()`.
Une voie marché n'apprend jamais d'un épisode contexte/graphe, et inversement.

Les features restent une **projection plate bornée**. Pas de GNN, pas de
message passing, pas d'agrégat de voisinage comme claim de graphe.

## Cutoff, I/O, flags

`WorldContextReader` n'est construit au boot que si le contexte est on (flag ou OU
YAML pilote). Le cycle trading ne freeze et n'enqueue que la cohorte marché.
L'attache contexte a lieu dans `WorldContextEpisodeEnricher`, l'attache graphe dans
`WorldGraphEpisodeEnricher`, avant `capture_and_predict`. Un échec
d'enrichissement est `partial` et laisse le marché continuer. Aucune I/O
réseau ou LLM dans ces enrichers. Le fetch macro est un worker
single-flight séparé, hors capture barre.

```bash
CASYS_WORLD_MODEL_CONTEXT_ENABLED=0          # défaut
CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED=0         # défaut
CASYS_WORLD_MODEL_GRAPH_ENABLED=0            # défaut
CASYS_WORLD_SHADOW_PILOT_ACTIVATION=1           # honore le YAML ; 0 saute l'auto-start
```

Desktop lit une projection **courante** de ces têtes publiées via
[l'explorateur World Graph](world-graph-explorer.md)
(`world_graph_explorer.v1`). Ce n'est pas un cutoff historique arbitraire.

Cette page **n'active rien**. Procédure : [opérer le World Model shadow](../../how-to/operate-world-model-shadow.md).
Ce document n'est ni une promotion, ni un changement d'autorité, ni un feu
vert trading.
