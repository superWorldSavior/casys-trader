# RFC — Extension company + secteurs + news du World Model (bridge sémantique)

- **Date** : 2026-09-16
- **Statut** : 💬 RFC proposée — prête pour revue ; aucune implémentation
- **Auteurs** : Erwan + Muse (+ revue Codex)
- **Portée** : nœuds `company`/`family`, writers `ISSUED_BY`/`MEMBER_OF_FAMILY`/`ABOUT`,
  états sémantiques versionnés (news, société, secteur) dans les signatures de patterns
- **Autorité** : `shadow_only` / `NO_GO` ; `decision_effect=none` ; inchangée
- **Registre** : complète D19 ; ne modifie ni l'autorité Trader ni les lanes market/context
- **Dépend de** : [graphe et hypothèses de patterns](2026-08-23-world-model-graph-pattern-hypotheses-design.md),
  [macro source-only](2026-08-23-world-model-macro-source-only-design.md),
  [cohorte prospective](2026-08-23-world-model-prospective-cohort-design.md)
- **Supersède** : aucun

## 1. Résumé et décision proposée

Le graphe live vérifié le 2026-09-16 contient 250 entités (0 `company`, 0 `family`),
8 617 relations (0 `ISSUED_BY`, `MEMBER_OF_FAMILY`, `ABOUT`), une identity map vide,
et des `OBSERVES` macro pointant monde/régions uniquement — jamais un pays.
Cinq hypothèses de patterns existent, avec 0 occurrence prospective et 0 lien d'outcome.
Pendant ce temps, 22 653 lignes de news Yahoo (~8 000 uniques, ~50 % périmées),
47 jours de briefs structurés et 365 dossiers d'intelligence société restent
invisibles au détecteur.

Pire : même peuplés, des writers `company`/`family` seuls ne produiraient aucun
pattern sectoriel, car les signatures de chemins sont sans identifiants
(`PatternStep.identity_tuple` : kinds + fraîcheur + `DriverState`, jamais
NASDAQ, USA ou « technologie »). Et tout `ABOUT` écrit aujourd'hui serait
hydraté en `DriverState.unspecified` (« une news existe », sans sémantique).

**Décision proposée** : traiter **company + secteurs + news comme une seule
extension**, dans cet ordre strict :

1. **Sémantique d'abord** : vocabulaires fermés versionnés (état news, état
   société, famille visible en signature) + hydration depuis les briefs.
2. **Writers ensuite** : nœuds `company`, `ISSUED_BY`, `MEMBER_OF_FAMILY`,
   `ABOUT` news/société — avec identités provisoires `issuer:` versionnées.
3. **Cohortes prospectives** « secteur × régime macro » et
   « classe d'événement × secteur × régime ».

Agrandir le graphe sans modifier sa sémantique de pattern est explicitement
hors scope : coût sans gain prédictif.

## 2. État vérifié (point de départ, 2026-09-16)

Les numéros de ligne ci-dessous datent du point de départ (pré-chantier).

| Fait | Source |
|---|---|
| Bootstrap seul writer structurel ; refuse les sociétés | `trader/application/world_model/ontology_bootstrap.py:117-130,149-150` |
| Entités live : 213 instruments, 17 venues, 16 pays, 3 régions, 1 monde | `state/world_model.db` |
| `OBSERVES` : 6 160, cibles monde/région uniquement | `state/world_model.db` |
| Hypothèses : 5 closes, 0 occurrence, 0 outcome link | `state/world_model.db` |
| `ABOUT` hydraté `unspecified` (pas de sémantique) | `trader/infrastructure/state_db/world_pattern_formation_query.py:565-572` |
| Chemins projetés : ancestrialité + branches + overlays `ABOUT`/`OBSERVES` | `trader/application/world_model/pattern_path.py:272-308` |
| News archivées : `{fetched_at, symbol, title, publisher, published_at, link, uuid}` | `trader/infrastructure/market_sources/news_feed.py:111-138` |
| Points brief : `symbols, severity, signal, horizon, direction, sources, source_refs` | `trader/domain/situation/brief.py:74-90` |
| Dossiers société : `issuer_identity` (statut), `business`, `financial_snapshot`, `earnings_and_guidance`, `company_thesis`, `catalysts`, `risks`, `source_refs`, `as_of` | `state/company_intelligence/current/*.json` (365) |
| Catalogue familles : `FAMILIES` + `family_for_symbol` (maintenu à la main) | `trader/domain/semantic/catalog.py:98,858` |
| Invariant têtes de scope : sous-ensemble toléré, contradiction refusée | `trader/application/world_model/graph_snapshot.py:140-152` |
| Vue bornée à la révision publiée (nouveaux heads ⇒ nouvelle révision) | `trader/application/world_model/graph_snapshot.py:155-189` |

## 3. Décisions de design

### D0. Révision étendue sans casser le bootstrap (point bloquant levé ici)

`derive_market_ontology` dérive les heads publiables **uniquement** du
`WorldScopeMapping`. Si un writer séparé publiait des heads `company`/`family`
dans une nouvelle révision, le prochain `ensure_published` verrait une révision
publiée ≠ dérivée et planifierait un `supersede` destructeur vers la version
mapping-only (perte des sociétés).

**Décision** : ne pas créer de writer latéral. Étendre la fonction de dérivation
en `dérivation(mapping + registre émetteurs + catalogue familles)`, chaque
entrée versionnée et hashée dans l'identité de révision. Le bootstrap reste
l'unique autorité de publication ; il refuse toujours toute inférence
(`company` n'entre que via le registre, jamais devinée). Le cycle
publish/supersede existant s'applique inchangé.

### D1. Identité `company` provisoire, versionnée, sourcée

Le schéma autorise déjà `lei:`, `cik:`, `issuer:`
(`trader/domain/world_graph.py:403-409`). En attendant LEI/CIK/MOPS :

- identité provisoire `issuer:yahoo:v1:<MIC>:<symbole>` (ex.
  `issuer:yahoo:v1:XCPH:CARL-B.CO`), construite uniquement depuis un brief
  société à `identity_status=verified` ;
- traçabilité : `source_refs` = brief(s) + archive news ; `effective_from` =
  disponibilité réelle (`as_of`/`ready_at`), jamais antidatée ;
- rapprochement multi-cotations/ADR plus tard via identity map
  (`WorldEntityIdentityLink`, liens même-kind, supersede append-only) ;
- table déterministe `exchange → MIC` (O1 **tranché à l'implémentation** :
  `yahoo_exchange_mic.v1`, 18 codes couvrant les 17 MIC du mapping, repli
  `TWO → XTAI` par convention mapping ; conflit ⇒ exclusion comptée,
  code inconnu ⇒ résolution `mapping_only`, jamais de devinette).

### D2. `MEMBER_OF_FAMILY` depuis un catalogue gelé et versionné

`FAMILIES` est maintenu à la main, sans version ni hash — inutilisable tel quel
en PIT. **Décision** : figer un `family_catalog.v1` (contenu + `content_sha256`
+ `effective_from`), consommé par la dérivation D0. Toute modification de
catalogue ⇒ nouvelle génération de révision (même mécanique que le mapping).
Couverture initiale mesurée contre les 213 instruments ; les symboles hors
catalogue n'obtiennent pas d'arête (pas de fallback silencieux).

### D3. Writers `ABOUT` : news et société, avec attribution prouvée

Trois garde-fous, non négociables (l'archive brute contient ~65 % de doublons
et ~50 % de lignes périmées, plus des collisions avérées type 2884.TW/Japon) :

1. **Points de briefs, pas titres bruts** : chaque `ABOUT` news cite un
   `SituationPoint` issu de `situation_notes` (`sources`, `source_refs`,
   `severity`, `signal`, `horizon`, `direction`) ; les points
   `is_operational=true` sont exclus quand le flag existe (le corpus
   `situation_notes` ne le persiste pas — limitation documentée).
2. **Déduplication par assertion datée** (amendé) : on ne bridge pas les
   articles bruts, donc la dédup `(symbole, UUID)` ne s'applique pas. Chaque
   point de brief est une assertion datée unique (`note_key` = clé naturelle,
   collision d'id + contenu divergent = conflit dur au store). Les doublons
   inter-briefs (paraphrases) restent un chantier futur, noté.
3. **Pas de gate horloge au build** (amendé) : la fenêtre de validité voyage
   avec l'artifact (`ready_at`/`valid_until` du brief, TTL 30 j pour les
   sociétés) ; l'expiration est appliquée à l'hydratation par cutoff.
   (Corrigé en revue backfill : le ledger reste PIT par date
   d'enregistrement — voir D6. Un artifact backfillé ne rejoint jamais ses
   cutoffs contemporains ; la fenêtre voyage quand même, pour borner
   l'hydratation forward.)
4. **Cibles** : `news_artifact → ABOUT → instrument` en v1 (aucun nœud
   `company` requis) ; `company_intelligence → ABOUT → company` plus
   `→ instrument` **systématiquement** (amendé en revue : chaque entrée du
   registre est une cotation liée par construction, `mapping_only` inclus —
   l'ancre mapping est l'autorité ; re-vérifier le hint `exchange` au bridge
   ne ferait que perdre du rappel sans gain de précision).
   Qualité d'attribution enregistrée (D4).
5. **Link step post-persistance** (ajouté en revue) : le bridge construit ses
   drafts pré-receipt (le receipt n'existe qu'une fois la ligne d'artifact
   durable) ; `AboutKnowledgeLink.from_persisted` attache ensuite la ref
   `<receipt_id>/<receipt_sha256>` (format owned par `about_receipt_ref`) à
   chaque relation, sans jamais re-timbrer `ontology_revision` (désaccord =
   bug appelant, levé ; mismatch receipt/enveloppe = donnée, `skipped` à
   raison fermée). Sans ce step, la gate anti-dérive du binder restait
   dormante sur nos propres écritures.

### D4. Sémantique pattern : le cœur de la RFC

Sans ceci, tout le reste est décoratif.

**D4a. `DriverState` étendu (fermé, versionné).** Nouveau `signal_class`
`news_state.v1` : `event_class` (résultats, guidance, acquisition, régulation,
opérationnel… — taxonomie fermée, point ouvert O2), `direction`, `strength`
(dérivé de `signal`/`severity`), `horizon_bucket` (fermé, dérivé de `horizon`
libre), `attribution_quality` (fermée). Nouveau `signal_class`
`company_state.v1` : `sector` (valeur `family_catalog.v1`), `thesis_status`,
`coverage_status`, `freshness_status` (réutiliser les statuts whitelistés du
contexte). `regime_bundle` macro inchangé. Hydration **des deux côtés** :
formation (`world_pattern_formation_query.py`) **et** évaluation
(`world_pattern_evaluation_query.py`) — sinon les occurrences prospectives ne
matcheront jamais.

**D4b. Famille visible en signature (amendé à l'implémentation : pas de bump).**
La signature reste ID-free sur les entités, mais la valeur taxonomique fermée
(`eu_tech`, …) entre dans l'identité du step `MEMBER_OF_FAMILY` (référence de
vocabulaire, pas identifiant d'instance), obligatoire sur ces steps, interdite
ailleurs. **Pas de bump de `GRAPH_PATH_RULE_VERSION`** : la règle est pinnée à
`traversal_policy_version` du config (`test_world_feature_contract.py:400`), et
un bump orphelinerait les 5 hypothèses existantes sans gain sémantique — les
données sans famille projettent à l'identique (`family_ref=None` des deux
côtés, reconstitution via `from_mapping`), et les futurs steps famille forment
naturellement de nouvelles signatures. La règle ne sera bumpée que si une même
signature change de sens.

**D4c. Symboles épinglés : autorisé, sous conditions.** L'utilisateur assume la
confiance aux symboles Yahoo. Un mode « entity-pinned » (symbole dans
l'identité) est admis uniquement avec : support minimal relevé par palier,
correction Holm-Bonferroni déjà prévue (`config/world_graph.yaml:56-59`),
gate derrière masque/feature-flag, et interdiction de conclure sur < N
événements distincts (N fixé dans le plan, proposition : 10).

## 4. Plan de mise en œuvre (ordre strict)

| Phase | Contenu | Sortie testable |
|---|---|---|
| P0 | Vocabulaires fermés `news_state.v1`, `company_state.v1`, `family_catalog.v1` (domain seul, stdlib) | tests limites : valeurs hors enum refusées, horizons libres buckettisés, `ABOUT` sans binding ⇒ rejet |
| P1 | Hydration formation + évaluation depuis briefs (sans bump de règle, cf. D4b) | formation sur données existantes : `ABOUT` projette des états non-`unspecified` |
| P2 | Dérivation étendue D0 + table exchange→MIC ; writers `company`/`ISSUED_BY`/`MEMBER_OF_FAMILY` | nouvelle révision publiée, invariant scope OK, bootstrap `ready` (pas `drifted`) |
| P3 | Bridge news `ABOUT` (dédup, fraîcheur, exclusion opérationnelle) | comptage : lignes → artifacts uniques → relations, avec rejets tracés |
| P4 | Cohortes prospectives « secteur × régime », « event_class × secteur × régime » | hypothèses non génériques, occurrences > 0 (prouvé sur données synthétiques ; activation live = décision ops séparée : boot étendu + backfill + cohorte) |

Chaque phase : tests limites d'abord (collisions, doublons, stale, multi-cotations,
hors-catalogue, `identity_status≠verified`), auto-revue exhaustive,
validation post-change. Aucune activation hors shadow.

### D5. Amendements de revue (post-implémentation, pré-activation)

- **Révision admise au bridge** : `ontology_revision` doit appartenir à la
  famille market ontology (`admits_market_ontology_family`), sinon le backfill
  verserait des relations silencieusement exclues des snapshots.
- **Jointures case-foldées** : l'identité ticker est insensible à la casse ;
  les index de jointure (notes→mapping, briefs→mapping/registre) foldent des
  deux côtés (`index_briefs_by_symbol` partagé, premier en ordre trié gagne
  une collision). Le catalogue, lui, normalise déjà (`normalize_symbol`).
- **Registre mapping-centrique** : on itère les entrées du mapping (autorité
  cotations) ; chaque entrée est liée ou exclue à raison fermée. Les briefs
  hors scope ne sont pas des exclusions (jamais revendiqués).
- **Corruption ≠ crash** : lignes corrompues sautées par les lecteurs
  (corpus), levées en `ValueError` par le writer (fail-loud, jamais de perte
  silencieuse) ; `worst_freshness` lève `ValueError` (pas `KeyError`) sur
  statut non rangé, avec test de contrat épinglant rangs = Literal.
- **Parseurs receipt intentionnellement miroirs** : le corpus duplique le
  parseur formation (cycle d'import sinon), en strictement plus fail-closed
  sur tokens malformés — documenté, pas factorisé.

### D6. Backfill forward-only + PIT record-time (revue backfill)

- Le ledger gate la disponibilité par **date d'enregistrement**
  (`receipt.ready_at`/`first_seen_at` vs cutoff) : un artifact ou un event
  écrit aujourd'hui est invisible aux cutoffs historiques, **by design**
  (garantie anti-lookahead ; les OBSERVES macro vivent déjà ainsi, tamponnés
  par génération). Conséquence : le backfill ne sert que le forward ; la
  formation historique ne verra jamais ces signaux ; les patterns se forment
  après accumulation shadow (semaines), pas sur réécriture du passé.
- Politique de sélection : **fenêtres live uniquement** (`valid_until`
  absente/inparseable, ou >= now) — les notes expirées ne pourraient jamais
  hydrater (fenêtre + receipt). Mesuré : 623/11168 notes, 557 drafts,
  744 relations ; + 243 briefs société (frais, TTL 30 j) → 486 relations.
  `--all` force le full (repro/debug).
- Le script (`scripts/backfill_world_knowledge_about.py`, dry-run par défaut,
  `--apply` pour écrire) est **le writer forward** : idempotent (no-op à
  contenu égal, conflit dur sinon), reprise auto via ids déterministes,
  self-healing (re-`ensure_published` étendu à chaque run, donc safe après
  édition du mapping), ABOUT sans fence.
- Activation = publication de la révision étendue : la capture live suit le
  **tip** (`ontology_revision=None` partout en chemin actif ; le pin YAML
  `market_ontology.v1` est dormant). Les 5 hypothèses existantes survivent
  (chemins d'ascendance projetés à l'identique, D4b). Les overlays restent
  scopés par génération (macro-identique, replay PIT préservé).
- Câblage requis côté query : `knowledge_root=state/world_knowledge` passé
  aux 5 constructions de sources (runtime ×2, CLI ×3) — no-op sans données.

### D7. Writers forward : publisher macro étendu + hook daemon (post-backfill)

- **4 publishers convergents, une seule fonction** : boot (`compose_world_ontology_attestation`),
  capture (`_compose_graph_capture`), bridge macro (`wire_world_macro_runtime`) et hook
  forward (`AboutForwardRunner`) dérivent tous le tip étendu depuis mapping + registre +
  catalogue **rechargés frais à chaque sweep**. Deux générations différentes ne peuvent pas
  coexister : un publisher legacy supersèderait le tip étendu puis se ferait re-superséder
  (oscillation). Preuve : 2 collects macro → 1 `Published`, 0 `Superseded`, tip == id étendu
  dérivé (`test_extended_bridge_publishes_one_stable_revision`).
- **Publisher macro** : `extended=True` + `briefs_root` câblés au daemon ; loads frais par sweep
  (`_load_extended_inputs`, même dérivation que boot/capture). Échec de load → bridge skippé
  **ce sweep uniquement** (erreur `stage=graph_bridge` tracée dans le rapport, statut `partial`,
  faits macro intacts) — jamais de dérivation legacy par défaut. `extended` sans `briefs_root`
  lève au wiring (fail-closed, comme la capture).
- **Spec du bridge admettant l'étendu** : `derive_macro_graph_bridge_spec` (et ses wrappers)
  accepte le legacy seul par défaut, et l'étendu uniquement quand registre + catalogue voyagent
  avec l'appel (sinon `ValueError`, fail-closed conservé). Le publisher macro les fait suivre.
- **Hook daemon event-driven** (rythme forward tranché : pas de cron) :
  `trader/runtime/world_about_runtime.py`, `AboutForwardRunner` chaîné sur `on_briefs_written`
  (news macro) et `on_brief_written` (company intelligence), après le refresh universe existant.
  Single-flight + coalescing (dernier trigger gagne), throttle 300 s par défaut
  (`CASYS_WORLD_ABOUT_FORWARD_MIN_INTERVAL_S`), kill-switch
  (`CASYS_WORLD_ABOUT_FORWARD_ENABLED`, défaut on), construit uniquement si le graphe est actif.
  Chaque sweep = recette backfill `--apply` sur inputs frais (notes live + briefs `current`),
  stores construits puis fermés dans le sweep (zéro partage inter-threads), fail-open
  (exception → rapport `error`, jamais propagée dans les threads LLM). Notes db absente →
  côté news skippé explicitement (`notes_source=missing`), côté société inchangé.
- **Arrêt propre** : `world_about_runner` ajouté aux ressources revendiquées du daemon et à
  `shutdown_runtime_resources` (best-effort, message `[world_about]`).

## 5. Points ouverts

- **O1** : table `exchange → MIC` déterministe — **tranché** : voir D1
  (18 codes, 17/17 MIC du mapping, `mapping_only` en dégradation).
- **O2** : valeurs fermées de `event_class` et `horizon_bucket` — **retranché
  (issue #23)** : l'analyste LLM émet `event_class` (enum fermée, champ requis
  par point), stocké en colonne `situation_notes.event_class`. Plus de
  re-dérivation : manquant → exclusion comptée `missing_event_class`,
  hors-enum → rejet loud (`invalid_event_class` au bridge,
  `missing_event_class` + `ValueError` au parse LLM). Zéro fallback keywords.
  Provenance explicite dans le bundle (`event_class_source`: `analyst` |
  `keywords`, `driver_news_bundle.v2` ; v1 migré en lecture avec `keywords`,
  fait historique). Table v1 (`news_event_class.v1`) conservée + testée pour
  documenter les artifacts déjà persistés (~59 % `unknown` mesurés, cause du
  retranchage).
- **O3** : seuils de support par palier (générique vs pinned) et N minimal
  d'événements distincts.
- **O4** : `PART_OF_WORLD` pays/venue→monde et `LOCATED_IN` venue→région direct
  restent schéma-only : élaguer ou justifier (hors scope, noté pour mémoire).
- **O5** : rythme du writer forward — **tranché** : hook daemon event-driven sur écriture
  de briefs + throttle (D7), pas de cron. Reste à calibrer en prod : `MIN_INTERVAL_S`
  (défaut 300 s) face à la cadence réelle des briefs LLM.
- **O6** : fast-path per-item — **implémenté (issue #24)** : le trigger porte
  l'identité (symbol / brief_ids), le worker écrit l'item seul sans throttle ;
  full sweeps throttlés en rattrapage (marqueur boot, intervalle 6 h
  `CASYS_WORLD_ABOUT_FULL_SWEEP_INTERVAL_S`, upgrade automatique). L'attestation
  fast-path dérive toujours depuis les inputs FULL (jamais de révision
  divergente). Nommé-mais-inécrivable → `skipped_symbols`/`skipped_brief_ids`
  (raisons fermées), jamais silencieux.

## 6. Non-objectifs

- Inférer des émetteurs sans source vérifiée (le refus du bootstrap reste).
- LEI/CIK/MOPS temps réel (rapprochement ultérieur via identity map).
- Effet décisionnel : tout reste `shadow_only` / `NO_GO`.
- Migration des 5 hypothèses existantes (cohortes séparées).

## 7. Critères d'acceptation

1. Une `company` n'existe que sourcée par un brief `verified` + MIC résolu.
2. Zéro `ABOUT` sans `DriverState` sémantique joint des deux côtés
   (formation et évaluation).
3. Le bootstrap post-extension rapporte `ready`, jamais `drifted`, sur la
   révision étendue.
4. Les doublons news et les lignes périmées sont comptés et rejetés, pas
   silencieusement absorbés.
5. Au moins une cohorte P4 produit des hypothèses distinguant secteurs ou
   classes d'événements (pas 5 variantes géo génériques).
6. Les 4 publishers dérivent le même tip étendu ; répéter un collect macro ne
   publie aucune révision supplémentaire (0 `Superseded`).
7. Chaque écriture de brief (news ou société) déclenche un sweep forward
   coalescé, throttlé, fail-open, sans bloquer les threads LLM.
