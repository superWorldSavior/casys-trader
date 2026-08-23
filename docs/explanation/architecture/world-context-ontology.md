# World Context / ontologie temporelle — spike prospectif V2

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
externe*, et aux capteurs analyste (macro, company) dont la disponibilité est
prouvée par un reçu sidecar. C'est de la **provenance** et de la
**traversée**. Ce n'est pas une découverte causale, pas un graphe de message
passing, et pas un agrégat familial.

## Question à laquelle il ne répond pas

Il ne dit pas qu'une association *cause* un mouvement de marché. Il n'y a pas
d'arête générique `CAUSES`. Il ne dit pas si un trade était bon, et il ne
produit ni PnL de portefeuille ni recommandation. Une ablation V2−V1 ne mesure
qu'un delta prédictif apparié (log-loss, Brier, accuracy/ECE), sans claim
économique ni claim causal.

La `venue` V1/V2 est aujourd'hui un scope marché logique (`EU`, `TW`, `US`) ;
ce n'est pas une MIC et il n'existe pas encore de taxonomie `region`
indépendante dans les sources. La RFC macro introduit donc un
`WorldScopeMapping` versionné/hashé qui résout `(market_venue, instrument)` vers
les scopes V3 canoniques (`mic`, pays, région). Aucun fallback `TW -> XTAI` ou
`US/EU -> une place arbitraire` n'est permis. Tant que ce mapping n'est pas
livré et gelé dans le manifeste, le raccord macro→graphe reste `NO_GO`.

## Cap produit : des cas sémantiques aux chaînes évaluées

Le cap n'est pas de remplacer les mémoires FLAIR / MemRL par un modèle
monolithique. Il est de les compléter par une représentation temporelle du
monde afin qu'un rappel ne porte plus seulement sur une phrase ou un cas
sémantiquement proche, mais puisse référencer une chaîne structurée, par
exemple :

```text
observation macro → région/pays ← place ← instrument cible
                                      ├→ famille
                                      └→ entreprise
instrument cible → outcome à horizon fixe
```

À terme, une telle chaîne doit être un objet versionné : entités et relations,
ordre temporel, preuves disponibles à chaque cutoff, hypothèse formulée,
prédiction, outcome observé, support, contextes de réussite et
contre-exemples. Le graphe porte les entités, relations et provenances ; le
baseline / GRU teste la valeur prédictive de la séquence ; FLAIR peut pondérer
les patterns confirmés par leurs outcomes ; MemRL peut juger l'utilité d'un
pattern effectivement rappelé dans une décision. Les rôles actuels de FLAIR
et MemRL restent ceux documentés dans
[`agent-knowledge-architecture.md`](../../reference/agent-knowledge-architecture.md)
et [`learnings-rag.md`](../../reference/learnings-rag.md).

Ce cap n'est **pas encore implémenté** : le V2 courant ne persiste ni objet
`CausalPattern`, ni score FLAIR/MemRL de chaîne, ni contrôle contrefactuel.
Avant contrôles négatifs, permutations temporelles et d'entités, situations
comparables sans le facteur supposé, et support prospectif suffisant, une
chaîne reste une **hypothèse causale / prédictive**, jamais une causalité
établie.

## Ce qui est collecté aujourd'hui

| Capteur | Source | Statut actuel |
|---|---|---|
| Marché OHLCV | capture V1 déjà gelée | contrôle, toujours émis |
| Macro régionale | `NewsMacroBrief` historique | **exclu** (`policy_contaminated`) : les briefs existants sont ancrés candidat / scope / company. Un rapport régénéré demain recevra un reçu d'availability valide mais restera exclu jusqu'à un producteur macro *source-only* |
| Company micro | `CompanyIntelligenceBrief` | éligible seulement si sidecar-prouvé **et** action-free ; projection bornée thesis / coverage / freshness / bucket de sources |
| Famille | métadonnée descriptive gelée dans l'observation V1 (`categorical_features.asset_family`) | **topologie descriptive** (`MEMBER_OF_FAMILY`) ; ce n'est pas une lookup du catalogue courant, ni la preuve qu'une relation catalogue était connue au cutoff. Si V1 n'a pas `asset_family`, il n'y a pas d'arête famille. Les boards / postures / agrégats familiaux sont des artefacts de politique et **ne sont pas** des features |

La prochaine phase utile est un producteur macro *source-only* vraiment
exogène, plus éventuellement un agrégat familial source-only. Ni l'un ni
l'autre n'existe aujourd'hui. Ne pas relâcher la deny-list de contamination.
Les contrats proposés et leur ordre d'exécution sont détaillés dans les RFCs :

- [flux macro source-only](../../superpowers/specs/2026-08-23-world-model-macro-source-only-design.md) ;
- [cohorte prospective appariée](../../superpowers/specs/2026-08-23-world-model-prospective-cohort-design.md) ;
- [graphe temporel et hypothèses de patterns](../../superpowers/specs/2026-08-23-world-model-graph-pattern-hypotheses-design.md).

## Histoire brute + reçus d'availability

Les JSONL canoniques (`news_briefs/YYYY-MM-DD.jsonl`,
`company_intelligence/history/{symbol}.jsonl`) restent des payloads bruts,
lisibles par les APIs héritées. La preuve de readiness n'est **pas** `as_of`,
pas un mtime, et pas une enveloppe imbriquée.

Séquence d'append :

1. écrire la ligne JSONL canonique ;
2. `flush` + `fsync` de l'histoire ;
3. **alors seulement** estampiller `ready_at` depuis une horloge injectée
   (UTC aware ; l'API publique n'accepte pas de `recorded_at` / `ready_at`
   caller-controlled) ;
4. append + `fsync` d'un reçu sidecar sous `availability_receipts/`.

Un crash avant le reçu laisse une ligne brute visible mais
`availability_unproven` pour la V2 — c'est sûr. Les lignes héritées sans
reçu matching restent lisibles et `availability_unproven`. Un reçu
forgé, tamperé ou dont le SHA-256 / id / scope ne revalide pas est
rejeté (fail closed). `published_at` n'est renseigné que si le payload a
un champ de publication explicite ; `as_of` n'en est pas un.

Le reçu est fsync *après* l'estampille `ready_at`. Un cutoff peut tomber
dans cette fenêtre : un poll ultérieur verrait sinon un reçu nouvellement
durable avec `ready_at <= cutoff`. `WorldContextReader` injecte une
horloge UTC aware et prime au boot tous les reçus déjà présents, mais
n'applique **pas** une horloge unique échantillonnée avant le scan. Le
scan est eager ; chaque reçu est estampillé `first_seen_at` au moment où
il a réellement été lu/observé (horloge injectée *après* découverte/lecture).
Un reçu append/fsync *après* le cutoff pendant ce scan a donc
`effective_ready_at` après cutoff et reste missing canonique neutre. Les
reçus déjà observés avant un cutoff restent éligibles selon leur première
observation effective :
`effective_ready_at = max(receipt.ready_at, first_seen_at)`. La preuve
modèle porte cette borne effective. Pas de mtime, pas de mutation des
sidecars. Un restart est volontairement conservateur pour les anciens
cutoffs, sauf si un épisode V2 canonique existe déjà.

## Records typés + projection NetworkX

Les records (`EntityRef`, `TopologyEdge`, `KnowledgeArtifact`,
`WorldContextSnapshot`) sont la vérité canonique : immuables en profondeur,
stdlib only, hash déterministe. `trader.infrastructure.graph` construit un
`MultiDiGraph` **frais** pour la traversée et la validation, clé par digest
d'arête afin de conserver des arêtes temporelles / provenance parallèles.
C'est une **projection de provenance / traversée**. Muter ce graphe ne
change pas les records. Aucun objet NetworkX n'est persisté. La topologie
n'est **pas** encodée dans le baseline ni le GRU, et l'ablation courante
**ne peut pas** revendiquer un impact de graphe. Il n'y a aujourd'hui
**aucun** consommateur de message passing ni d'agrégation sur ce graphe.

La topologie familiale n'est pas une lookup du catalogue courant : c'est une
métadonnée descriptive déjà gelée dans l'observation V1
(`categorical_features.asset_family`). Elle ne prouve pas qu'une relation
catalogue était connue au cutoff. Si cette clé est absente, il n'y a pas
d'arête `MEMBER_OF_FAMILY`. La place (`venue`) vient de la capture. Une arête
`ISSUED_BY` n'existe que pour une identité émetteur `verified` munie d'un
identifiant externe namespacé. Le V2 livré peut encore rejouer son fallback
historique (`lei`, `cik`, puis `isin`), mais la RFC V3 interdit de promouvoir
un ISIN en identité d'entreprise : l'ISIN identifie l'instrument ; l'entreprise
utilise LEI, CIK ou un provider issuer ID approuvé. Un `issuer_name` n'est
jamais un identifiant : les homonymes se fusionneraient.

## V1 gelée, V2 opt-in, quatre voies isolées

| Voie | Contrat | Identité |
|---|---|---|
| Markov marché | `market_ohlcv_causal.v1` | `hierarchical_dirichlet_world_baseline@v1` |
| GRU marché | `market_ohlcv_causal.v1` | `online_gru_world_challenger@v1` |
| Markov contexte | `market_ohlcv_context.v2` | `hierarchical_dirichlet_world_baseline@context.v2` |
| GRU contexte | `market_ohlcv_context.v2` | `online_gru_world_challenger@context.v2` |

La V1 reste immuable : mêmes payloads, mêmes `episode_id`, même empreinte de
features, mêmes identités de modèles. La V2 dérive l'observation de marché du
**même slot de barre**, y attache un snapshot de contexte au cutoff
d'achèvement de barre (pas l'heure de poll), et porte une identité distincte
qui inclut le digest du snapshot. Le même OHLCV refetché à `T+5m` doit
reproduire le même ID V2 ; un OHLCV différent sur le même slot conflict.

La persistance V2 est canonical-first-write. Un trigger SQLite `BEFORE INSERT`
(et non un index unique partiel) refuse atomiquement un second payload ou
`episode_id` sur un slot équivalent
`(venue, symbol, bar_interval, as_of_bar_ts, feature_contract_version,
sampling_policy_version)` où `feature_contract_version='market_ohlcv_context.v2'`.
Les écritures V2 nouvelles passent par `WorldEpisode` : le texte d'ancre
stocké est UTC canonique, fractions conservées. Le trigger compare ce
texte exact, donc `.100000` et `.900000` restent des slots distincts,
tandis que `Z` / `+00:00` se canonicisent avant insert. Pour les lignes
héritées (migration v1), le lookup charge les candidats des autres
dimensions de slot (`ORDER BY recorded_at, episode_id`) et compare
`as_of_bar_ts` en Python avec `parse_utc_timestamp` : `+0000` / `+00:00` /
`Z` sont équivalents sans muter les lignes append-only, et la précision
fractionnaire est préservée. Un replay exact reste un no-op. Les lignes
V2 héritées dupliquées ne sont ni mutées ni supprimées : le lookup
retourne déterministiquement la première ligne canonique. Le service
réutilise ce premier épisode, y compris après restart, en projetant
l'évidence marché sur le contrat `WorldEpisode` (garde enveloppe /
observation) puis en comparant OHLCV / features / éligibilité sans le
contexte, `context_id`, ni les horloges de fetch.

Chaque entrée publique baseline / GRU enforce `accepts_episode()`. Une voie
V1 n'apprend jamais d'un épisode V2, et inversement.

Les features V2 actuelles sont une **projection plate bornée** des catégories
de contexte. Pas de GNN, pas de message passing, pas d'agrégat de voisinage.
L'incrément V2 mesurable aujourd'hui est uniquement les métadonnées company /
status compactes **causalement prouvées** plus la missingness et la
topologie familiale déjà partagées avec la V1. Aucun claim causal, PnL ou
de performance.

## Cutoff et I/O

Le cutoff de contexte est l'horloge d'achèvement de la barre :

- `bar_close` → timestamp d'ancre ;
- `bar_start` → ancre + intervalle parsé ;
- inconnu / inparsable → pas d'attache V2 (fail closed, jamais de guess).

Il doit rester `<= observation.available_at`. On ne réécrit pas le
`available_at` marché V1 (heure de fetch).

`WorldContextReader` n'est construit qu'au boot si le flag V2 est on. Le
cycle trading ne freeze et n'enqueue que la cohorte V1. L'attache V2 a lieu
dans le worker background (`WorldContextEpisodeEnricher`) avant
`capture_and_predict`. Un échec d'enrichissement est `partial` et laisse la
V1 continuer. Aucune I/O réseau ou LLM.

## Flag, défaut off, pas de changement runtime maintenant

```bash
CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED=0   # défaut
```

Cette page **n'active rien** et ne redémarre pas le daemon live.
Le code V2 reste opt-in et doit démarrer une **cohorte prospective propre**,
pilotée par un manifeste et des événements de domaine, conformément à la
[RFC cohorte](../../superpowers/specs/2026-08-23-world-model-prospective-cohort-design.md).
Activation et interprétation restent `NO_GO` tant que les gates causales
ci-dessus ne passent pas **et** qu'un capteur macro source-only n'existe pas.

Procédure d'activation **opérateur** (le flag n'est lu qu'au boot) :

1. Ne pas toucher au process déjà lancé.
2. Poser `CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED=1` dans l'environnement du
   *prochain* process daemon (`.env` / unit / launcher).
3. Au prochain arrêt/relance volontaire du daemon, le boot instancie les
   quatre voies et l'enricher local. Toujours `shadow_only` / `NO_GO`.
4. Un export dans un shell courant ne change pas un daemon vivant.

Voir [opérer le World Model shadow](../../how-to/operate-world-model-shadow.md).
Ce document n'est ni une promotion, ni un changement d'autorité, ni un feu
vert trading.
