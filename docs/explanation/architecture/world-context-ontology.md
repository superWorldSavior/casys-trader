# World Context / ontologie temporelle — V2 et ombre V3

> **Type** : Explanation (Diataxis). Capacité **shadow-only** et **fail-open**
> pour le trading, **fail-closed** pour la causalité et l'apprentissage
> inter-voies. Elle n'a aucune autorité de trading et n'est jamais promue
> automatiquement. Le contrat marché V1 reste
> [world-model-shadow](world-model-shadow.md).

## Question à laquelle le graphe répond

Le graphe d'ontologie dit, pour un cutoff marché **prouvé** :

> « Qu'est-ce qui était connu, à propos de quelle entité, depuis quelle
> source, au plus tard à ce cutoff ? »

Il relie un instrument à sa place, éventuellement une famille déjà gelée dans
l'observation marché V1, éventuellement un émetteur *vérifié par identifiant
externe*, et aux capteurs (macro source-only, company) dont la disponibilité
est prouvée par un reçu. C'est de la **provenance** et de la **traversée**.
Ce n'est pas une découverte causale, pas un graphe de message passing, et
pas un agrégat familial.

## Question à laquelle il ne répond pas

Il ne dit pas qu'une association *cause* un mouvement de marché. Il n'y a
pas d'arête générique `CAUSES` (ni `HYPOTHESIZED_INFLUENCE` persistée comme
fait). Il ne dit pas si un trade était bon, et il ne produit ni PnL de
portefeuille ni recommandation. Une ablation appariée ne mesure qu'un delta
prédictif (log-loss, Brier), sans claim économique ni claim causal.

La `venue` V1/V2 est un scope marché logique (`EU`, `TW`, `US`). Le mapping
versionné `world_scope_mapping.v2` (hash gelé dans le YAML pilote ; successeur
append-only de `world_scope_mapping.v1`) résout `(market_venue, instrument)`
vers les scopes canoniques (`mic`, pays, région). Aucun fallback `TW -> XTAI`
silencieux. Un scope `unmapped` ou `ambiguous` produit une missingness
canonique, pas une invention. L'ontologie structurelle commitée est
`market_ontology.v2` : un ledger qui a déjà publié `market_ontology.v1` la
supersède, il ne réécrit pas `v1`.

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
les anciens cutoffs, sauf si un épisode V2/V3 canonique existe déjà.

Cutoff de contexte V2/V3 = horloge d'**achèvement de barre** :

- `bar_close` → timestamp d'ancre ;
- `bar_start` → ancre + intervalle parsé ;
- inconnu / inparsable → pas d'attache (fail closed, jamais de guess).

Il doit rester `<= observation.available_at`. On ne réécrit pas le
`available_at` marché V1 (heure de fetch).

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
| Marché OHLCV | capture V1 déjà gelée | contrôle, toujours émis si ancre valide |
| Macro source-only | `MacroWorldObservation` (`macro_source_only.v2`) | éligible si envelope + reçu PIT **et** producteur admis `v2` ; worker opt-in / OR YAML. `v1` reste parseable, hors ombre. |
| Macro Univers | `NewsMacroBrief` | **exclu** comme source modèle World Context (`policy_contaminated`) |
| Événements GDELT | `state/gdelt/` | **exclu** du registre macro source-only |
| Company micro | `CompanyIntelligenceBrief` | éligible seulement si sidecar-prouvé **et** action-free |
| Famille | `categorical_features.asset_family` gelé dans V1 | topologie descriptive `MEMBER_OF_FAMILY`, pas une lookup catalogue courante |

`WorldContextReader.lookup_macro` consomme uniquement les envelopes
source-only. Le constructeur peut encore recevoir un `news_store` pour
compatibilité : ce n'est **pas** une source modèle. GDELT continue d'alimenter
l'analyste Univers ; ça ne le réintroduit pas dans le World Context.

Allowlist macro actuelle : DBnomics (taux, CPI, chômage) et Yahoo Finance
(brent, or). Exclus aussi : FRED, ecbdata, OECD. Gaps déclarés (USD large,
taux/CPI Taiwan, séries de croissance) restent des missingness honnêtes,
jamais une valeur nulle inventée.

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
Markov/GRU V1 ; la voie V3 encode un vecteur borné (`topology_status_only`
vs `graph_content`), pas un GNN.

Relations structurelles : `PART_OF_WORLD`, `LOCATED_IN`, `TRADED_ON`,
`ISSUED_BY`, `MEMBER_OF_FAMILY`. Connaissance : `ABOUT`, `OBSERVES`,
`DERIVED_FROM`, `SUPERSEDES`, `USES`. Interdites : `CAUSES`, `CAUSE`,
`CAUSED_BY`, `CAUSAL`, `HYPOTHESIZED_INFLUENCE`.

Budgets de traversée (YAML + overlay status) : profondeur 4, 32 chemins
par racine, cycles interdits. Un snapshot `missing` / `stale` / budget
dépassé s'encode en missingness ; il ne droppe pas le V1.

L'émetteur : LEI, CIK ou provider `issuer:` approuvé. Un ISIN identifie
l'instrument, jamais l'entreprise. Un `issuer_name` n'est pas un
identifiant.

`config/world_graph_v3.yaml` conserve `cohort_id: null` (C1 n'a pas de
voies graphe). `compose_local_graph_v3_lanes(..., study_cohort_id=...)`
injecte l'id de la cohorte `graph_v3` matérialisée au boot. Le YAML n'est
pas une autorité de cohorte.

Un graphe **câblé** au boot n'écrit rien (`writes=none_until_due_cycle`).
Un cycle idle ou un instrument unmapped peut écrire du V1 et **zéro**
compagnon V3.

## V1 gelée, V2 opt-in, V3 cohorte séparée

| Voie | Contrat | Identité typique |
|---|---|---|
| Markov / GRU marché V1 | `market_ohlcv_causal.v1` | baseline / challenger historiques |
| Markov / GRU contexte V2 | `market_ohlcv_context.v2` | `context.v2` ou `context.v2.macro_source.v2` si le store macro est câblé |
| Lanes C1 | masques market / status / company / macro / joint | GRU froid `sequence_length=4` |
| Markov / GRU graphe V3 | `market_ohlcv_graph.v3` | `topology_status_only` vs `graph_content` |

La V1 reste immuable : mêmes payloads, mêmes `episode_id` marché. La V2
dérive l'observation du **même slot de barre**, y attache un snapshot au
cutoff d'achèvement de barre, identité distincte incluant le digest du
snapshot. Le V3 attache **un** compagnon graphe par slot V1, sans élargir
V1/V2.

La persistance V2/V3 est canonical-first-write. Un replay exact reste un
no-op. Chaque entrée publique baseline / GRU enforce `accepts_episode()`.
Une voie V1 n'apprend jamais d'un épisode V2/V3, et inversement.

Les features restent une **projection plate bornée**. Pas de GNN, pas de
message passing, pas d'agrégat de voisinage comme claim de graphe.

## Cutoff, I/O, flags

`WorldContextReader` n'est construit au boot que si V2 est on (flag ou OU
YAML pilote). Le cycle trading ne freeze et n'enqueue que la cohorte V1.
L'attache V2 a lieu dans `WorldContextEpisodeEnricher`, l'attache V3 dans
`WorldGraphEpisodeEnricher`, avant `capture_and_predict`. Un échec
d'enrichissement est `partial` et laisse la V1 continuer. Aucune I/O
réseau ou LLM dans ces enrichers. Le fetch macro est un worker
single-flight séparé, hors capture barre.

```bash
CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED=0          # défaut
CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED=0         # défaut
CASYS_WORLD_MODEL_GRAPH_V3_ENABLED=0            # défaut
CASYS_WORLD_SHADOW_PILOT_ACTIVATION=1           # honore le YAML ; 0 saute l'auto-start
```

Cette page **n'active rien**. Procédure : [opérer le World Model shadow](../../how-to/operate-world-model-shadow.md).
Ce document n'est ni une promotion, ni un changement d'autorité, ni un feu
vert trading.
