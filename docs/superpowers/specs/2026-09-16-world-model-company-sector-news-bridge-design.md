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
- table déterministe `exchange → MIC` requise (point ouvert O1) : sans MIC
  résolu, pas de nœud instrument cible, donc pas d'`ISSUED_BY` (fail-closed).

### D2. `MEMBER_OF_FAMILY` depuis un catalogue gelé et versionné

`FAMILIES` est maintenu à la main, sans version ni hash — inutilisable tel quel
en PIT. **Décision** : figer un `family_catalog.v1` (contenu + `content_sha256`
+ `effective_from`), consommé par la dérivation D0. Toute modification de
catalogue ⇒ nouvelle génération de révision (même mécanique que le mapping).
Couverture initiale mesurée contre les 213 instruments ; les symboles hors
catalogue n'obtiennent pas d'arête (pas de fallback silencieux).

### D3. Writers `ABOUT` : news et société, avec attribution prouvée

Trois garde-fous, non négociables (l'archive contient ~65 % de doublons et
~50 % de lignes périmées, plus des collisions avérées type 2884.TW/Japon) :

1. **Déduplication globale** par `(symbole, UUID)` + fenêtre de fraîcheur
   stricte (seuil configuré, refus au-delà, pas de dégradé silencieux).
2. **Points de briefs, pas titres bruts** : chaque `ABOUT` news cite un
   `SituationPoint` (`sources`, `source_refs`, `severity`, `signal`,
   `horizon`, `direction`) ; les points `is_operational=true` sont exclus.
3. **Cibles** : `news_artifact → ABOUT → instrument` en v1 (aucun nœud
   `company` requis) ; `company_intelligence → ABOUT → company` plus
   `→ instrument` quand le point concerne précisément la cotation
   (devise, guidance par action…). Qualité d'attribution enregistrée (D4).

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

**D4b. Famille visible en signature.** La signature reste ID-free sur les
entités, mais la valeur taxonomique fermée (`eu_tech`, …) entre dans l'identité
du step `MEMBER_OF_FAMILY` (référence de vocabulaire, pas identifiant
d'instance). Bump de `GRAPH_PATH_RULE_VERSION` ; anciennes signatures
non comparables (cohortes séparées, pas de migration).

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
| P1 | Hydration formation + évaluation depuis briefs ; bump `GRAPH_PATH_RULE_VERSION` | formation sur données existantes : `ABOUT` projette des états non-`unspecified` |
| P2 | Dérivation étendue D0 + table exchange→MIC ; writers `company`/`ISSUED_BY`/`MEMBER_OF_FAMILY` | nouvelle révision publiée, invariant scope OK, bootstrap `ready` (pas `drifted`) |
| P3 | Bridge news `ABOUT` (dédup, fraîcheur, exclusion opérationnelle) | comptage : lignes → artifacts uniques → relations, avec rejets tracés |
| P4 | Cohortes prospectives « secteur × régime », « event_class × secteur × régime » | hypothèses non génériques, occurrences > 0 |

Chaque phase : tests limites d'abord (collisions, doublons, stale, multi-cotations,
hors-catalogue, `identity_status≠verified`), revue Codex pré-commit, validation
post-change. Aucune activation hors shadow.

## 5. Points ouverts

- **O1** : table `exchange → MIC` déterministe (sources : briefs `exchange`/`venue`
  + mapping existant ; conflits ⇒ fail-closed, pas de devinette).
- **O2** : valeurs fermées de `event_class` et `horizon_bucket` (proposition de
  départ : earnings, guidance, M&A, régulation, opérationnel, macro-société ;
  buckets 0-4h / 4-24h / 1-7d / périmé).
- **O3** : seuils de support par palier (générique vs pinned) et N minimal
  d'événements distincts.
- **O4** : `PART_OF_WORLD` pays/venue→monde et `LOCATED_IN` venue→région direct
  restent schéma-only : élaguer ou justifier (hors scope, noté pour mémoire).

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
