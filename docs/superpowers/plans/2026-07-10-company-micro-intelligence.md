# Plan d'implémentation — analyste micro et intelligence entreprise par symbole

> **Statut : implémenté le 2026-07-10.**
> **Décision métier proposée : D16.**
> **Dépend de :** D15 (pool candidat news-aware, agent Univers seul propriétaire
> de la hotlist) et de
> `docs/superpowers/specs/2026-07-09-universe-intelligence-pass-design.md`.
> Les lots A à D sont branchés ; le mode trader reste intentionnellement
> `observe`. Référence opérateur : `docs/reference/company-intelligence.md`.

## 1. Résultat attendu

Ajouter une passe de recherche entreprise durable, sourcée et versionnée par
symbole, disponible avant la sélection finale :

```text
sources entreprise / filings / earnings
                  ↓
       analyste micro par symbole
                  ↓
     CompanyIntelligenceBrief courant
             ↙                 ↘
ancre pour analyste news    contexte pour agent Univers
                                  ↓
                       hotlist + rationale/mandat
                                  ↓
                     tranche symbole au trader
```

Le lot principal est terminé quand l'agent Univers consomme le contexte micro
de tous les candidats et trace les versions exactes utilisées. Le raccord au
trader est inclus dans le même plan, mais comme tranche suivante indépendante et
réversible, d'abord en mode `observe`.

## 2. Décisions de responsabilité

1. Le radar et le scout construisent le pool candidat. Ils ne font pas de
   recherche fondamentale.
2. L'analyste micro décrit l'entreprise et produit un avis analytique borné :
   `supports_selection`, `neutral`, `argues_against` ou
   `insufficient_evidence`. Il ne retourne jamais `add`, `remove`,
   `selected_hotlist`, ordre, quantité, stop ou sizing.
3. L'analyste macro/news reste le digesteur de la situation périssable. Il peut
   lire une ancre micro pour interpréter une news, mais il ne réécrit pas le
   dossier entreprise à chaque journée.
4. L'agent Univers reste seul propriétaire de la hotlist. Aujourd'hui il
   sélectionne et rationalise ; il ne produit pas encore de décision de trading.
5. Le trader reste seul propriétaire de `OPEN/HOLD/WATCH/REDUCE/CLOSE`, du
   timing technique, des niveaux et du sizing. Il recevra une tranche compacte
   du mandat Univers activé et du dossier micro, jamais la recherche complète.
6. Une entreprise solide et une action attractive sont deux questions
   différentes. Le contrat sépare `company_thesis_status` de
   `security_readiness`; l'analyste micro ne fabrique pas une recommandation de
   titre sans prix, attentes et contexte de marché suffisants.

Cette mémoire par symbole ne réintroduit pas `learnings.by_symbol` : elle porte
des faits émetteur et une thèse sourcée, pas une « manière de trader AAPL ».

## 3. Vérité actuelle à préserver

- `NewsMacroBrief` est périssable, par venue et par date ; il est lié à un
  `candidate_scope_id` exact.
- `situation_memory.db` est un index dérivé des points news ; ce n'est pas un
  dossier société et il ne faut pas le recycler comme tel.
- `UniverseCompositionRequest` reçoit candidats, baseline, sticky, régime,
  contexte famille et brief news, mais aucun contexte entreprise durable.
- `UniverseAgentDecision` retourne actuellement `selected_hotlist`, `summary`,
  `family_postures` et `symbol_rationales`, sans trade/stop/qty.
- Le trader ne voit actuellement ni brief news, ni rationale Univers, ni mandat.
  Le `brief_ref` ajouté par le recorder l'est après la décision et ne prouve pas
  que le modèle l'a vu.
- L'état live local ne contient pas encore de `candidate_scopes`,
  `universe_runs` ou `universe_prepared`. Un bootstrap explicite est nécessaire
  pour rendre la nouvelle capacité disponible sans attendre un prochain
  close/pré-open.

## 4. Contrats métier

### 4.1 Deux artefacts séparés

`CompanyEvidenceSnapshot` est déterministe et normalisé par le code : identité,
périodes, unités, métriques, événements, source et fraîcheur. Le LLM ne devient
jamais la source d'un chiffre.

`CompanyIntelligenceBrief` est la synthèse du micro-analyste. Chaque affirmation
doit référencer des éléments du catalogue d'évidence fourni ; les affirmations
sans référence valide sont supprimées comme dans l'analyste macro/news.

### 4.2 Enveloppe du brief

Le modèle pur vit dans `trader/domain/company/intelligence.py` et contient au
minimum :

```json
{
  "schema_version": 1,
  "brief_id": "company_micro:v1:SAP.DE:<hash>",
  "symbol": "SAP.DE",
  "issuer_identity": {
    "issuer_name": "SAP SE",
    "instrument_type": "equity",
    "exchange": "XETRA",
    "external_ids": {"lei": "..."},
    "identity_status": "verified"
  },
  "as_of": "...",
  "input_signature": "...",
  "coverage": {
    "status": "full|partial|missing|unsupported",
    "sections": {}
  },
  "business": {},
  "financial_snapshot": {},
  "earnings_and_guidance": {},
  "company_thesis": {
    "status": "strengthening|intact|watch|impaired|broken|untested",
    "pillars": [],
    "confirming_evidence": [],
    "disconfirming_evidence": [],
    "kill_criteria": [],
    "next_proof_points": []
  },
  "catalysts": [],
  "risks": [],
  "open_questions": [],
  "selection_view": {
    "posture": "supports_selection|neutral|argues_against|insufficient_evidence",
    "confidence": "low|medium|high",
    "reasons": [],
    "scope": "universe_surveillance"
  },
  "security_readiness": "not_evaluated|conditional|not_decision_grade",
  "source_refs": []
}
```

Les observations utilisent un type commun borné `SourcedCompanyPoint` : texte,
`source_refs`, label d'évidence, confiance, période et horizon. Les tailles de
listes et de texte sont forcées dans le domaine, pas seulement annoncées dans le
prompt.

### 4.3 Fraîcheur par section

Il n'existe pas de TTL quotidien global. Chaque section porte :

- `source_as_of` ;
- `verified_at` ;
- `refresh_after` ;
- `freshness = fresh|stale|missing|unknown` ;
- la raison de refresh et les sources qui la rendront à nouveau actuelle.

L'identité et le business bougent lentement ; un nouveau filing, earnings ou
guidance invalide immédiatement les sections concernées. La thèse est recalculée
sur changement de l'`input_signature`, avec une revue de sécurité périodique
configurable. Le contexte prix/valorisation à courte durée de vie ne doit pas
rendre tout le dossier quotidien : il reste dans le radar/Univers ou dans une
section séparée explicitement datée.

## 5. Persistance par symbole

La vérité canonique est un historique append-only **adressé par symbole**, pas
une partition par journée :

```text
state/fundamental_items/<symbol_hash>.jsonl
state/company_intelligence/history/<symbol_hash>.jsonl
state/company_intelligence/current/<symbol_hash>.json
state/company_analysis_runs/YYYY-MM-DD.jsonl
state/company_analysis_runs/latest/<symbol_hash>.json
state/company_sources/<content_hash>.*
```

`symbol_hash = sha256(symbol)` évite collisions et traversées de chemin pour les
tickers exotiques. Le symbole exact reste dans chaque payload et doit
correspondre à la clé demandée à la lecture.

- `fundamental_items` contient les observations officielles normalisées,
  dédupliquées par `item_id + content_hash` ;
- `company_intelligence/history` contient chaque version complète du brief ;
- `current` est une projection atomique reconstructible depuis l'historique ;
- `company_analysis_runs` trace déclencheur, inputs, statut, erreur, latence,
  modèle, version produite et scopes demandeurs ;
- `company_sources` garde les documents/extractions lourdes content-addressed ;
  les briefs n'en recopient que les références et hashes.

Invariants du store :

- les versions sont immuables et append-only ;
- même `(symbol, input_signature, depth)` = idempotence ;
- l'échec d'un refresh ne remplace jamais le dernier bon brief ;
- l'append canonique précède toujours le remplacement atomique de la projection
  courante ; un crash ne peut donc pas faire pointer vers une version absente ;
- lecture unitaire et `read_current_many(symbols, at=...)` pour l'Univers ;
- les ETF/indices sont `unsupported_instrument_type`, jamais analysés comme une
  société ;
- une résolution ticker/émetteur ambiguë produit `identity_mismatch`, jamais une
  attribution silencieuse à la mauvaise entreprise.

Fichiers :

- nouveaux `trader/infrastructure/state_db/fundamental_item_store.py`,
  `company_intelligence_store.py` et `company_analysis_run_store.py` ;
- nouveau `tests/state_db/test_company_intelligence_store.py` ;
- tests append-only, écriture atomique, reconstruction, idempotence, corruption
  et bulk-read. SQLite reste optionnel plus tard comme index dérivé, jamais comme
  seule vérité de recherche.

## 6. Sources et normalisation

Définir les ports dans `trader/application/analyst/company_micro.py`. Les
adaptateurs concrets restent sous
`trader/infrastructure/market_sources/company/` :

- couverture transverse immédiate : yfinance, déjà dépendance du repo, comme
  source standardisée/fallback explicitement partielle ;
- US : SEC EDGAR / Company Facts / filings comme source primaire ;
- TW : FinMind avec références MOPS/TWSE lorsque disponibles ;
- EU : ESEF via filings.xbrl.org ;
- calendrier/earnings et news entreprise : réutiliser les artefacts locaux
  existants lorsqu'ils sont présents.

Ajouter `config/company_intelligence.yaml` pour l'ordre des providers, les
cadences de probe, les timeouts, la concurrence research et les budgets de
projection. Ajouter `config/company_entities.yaml` uniquement pour les
exceptions d'identité externes impossibles à résoudre automatiquement
(CIK/LEI/ISIN ou code MOPS) ; ne pas y stocker de faits de recherche manuels.

Chaque provider retourne une `CompanyEvidenceSnapshot`. La normalisation
vérifie entité, période, unité, devise et source. Les tests reposent sur des
fixtures enregistrées et n'utilisent jamais le réseau.

Aucun fetch réseau ni parsing de filing ne se produit dans le cycle de trading,
le prompt Univers ou le prompt trader.

## 7. Agent micro

Créer :

- `trader/agent/company_micro/prompt.py` ;
- `trader/agent/company_micro/analyzer.py` ;
- `trader/agent/company_micro/__init__.py` ;
- `tests/test_company_micro_analyst.py` ;
- `tests/application/test_company_micro_analysis.py` ;
- `tests/domain/company/test_intelligence.py`.

Le use case reçoit un seul symbole et un paquet d'évidence normalisé. Le code
force `symbol`, `as_of`, `input_signature`, identité et catalogue de sources. Il
revalide toutes les `source_refs`, supprime les points non sourcés et rejette les
champs d'autorité interdits (`add/remove`, `selected_hotlist`, ordre, stop, qty,
sizing).

Deux profondeurs réutilisent le même historique :

- `screen` : dossier compact requis avant sélection pour tous les candidats ;
- `deep` : approfondissement asynchrone pour les symboles sélectionnés et les
  sticky, sans bloquer l'activation.

Le noyau V1 reprend les concepts utiles du plugin financier : baseline
`company-tearsheet` et suivi falsifiable `thesis-tracker`. DCF, modèle trois
états, comps complets et target price sont hors V1.

## 8. Scheduling durable et disponibilité immédiate

Utiliser `state/company_research_tasks.db` avec un `TaskLedger` et un pool dédiés
au research, séparés des pools `decide` et `execute`. Le `claim()` actuel ne
filtre pas le kind : partager le même ledger avec des handlers différents
risquerait qu'un worker research claim une décision de trading.

Contrat des tâches :

- `kind=company_micro` ;
- `partition_key=symbol` ;
- `dedup_key=symbol:input_signature:depth` ;
- priorité : position/sticky, nouveau filing/earnings/guidance, challenger
  fresh-news, candidat manquant/stale, puis approfondissement ;
- retries/backoff et états `pending/running/done/dead` existants ;
- concurrence bornée par la ressource research, jamais par une troncature du
  nombre de candidats.

Créer `trader/runtime/company_intelligence_runtime.py`, branché au boot, aux
triggers post-cycle et à `runtime_shutdown`. Un résultat micro nouveau doit
re-déclencher/coalescer la préparation Univers ; l'ordre de lancement de deux
threads n'est pas une dépendance fiable.

Déclencheurs : nouveau symbole, section stale, source fingerprint modifié,
filing/earnings/guidance, challenger nouveau ou événement news matériel.

Pour éviter un cold start à la première fenêtre pré-open :

1. au boot, charger les derniers scopes disponibles, l'univers actif et les
   sticky, puis enfiler les dossiers manquants/stale ;
2. accepter le parent `scope_phase=close` pour préparer le top 40 plusieurs
   heures avant l'ouverture ;
3. à la création de l'enfant pré-open, enfiler les challengers nouveaux ;
4. fournir `casys-trader company-intelligence refresh --scope current --wait`
   et `... status` pour backfill/opérations ;
5. si aucun scope récent n'existe encore, `--scope current` utilise l'univers
   actif + sticky et signale explicitement que la couverture candidate complète
   attend le prochain scope.

Kill switch : `CASYS_COMPANY_MICRO_ANALYST_ENABLED=0`.

## 9. Projection vers l'analyste macro/news

Après validation du store :

- ajouter à `NewsMacroAnalysisRequest` des ancres micro compactes pour les
  candidats ayant une news observée ou une évidence challenger ;
- inclure les `company_brief_refs` exactes dans `input_refs` et la signature ;
- un changement de brief entreprise est une entrée matérielle et contourne le
  cooldown de refresh news ordinaire ;
- le prompt news utilise l'ancre pour dire si l'événement confirme, infirme ou
  change la thèse ; il ne relit pas le filing et ne met pas à jour le store micro ;
- conserver la chaîne de provenance brief micro -> sources d'origine.

Fichiers principaux :

- `trader/application/analyst/news_macro.py` ;
- `trader/runtime/news_macro_runtime.py` ;
- `trader/agent/news_macro/prompt.py` ;
- tests news macro existants.

## 10. Projection vers l'agent Univers

Ajouter un `UniverseCompanyContext`, distinct de
`UniverseSituationContext`. Chaque candidat reste représenté au minimum par :

- `status = fresh|partial|stale|missing|unsupported|identity_mismatch` ;
- `brief_ref`, `as_of` et fraîcheur ;
- `company_thesis_status` et `selection_view` ;
- résumé, deux moteurs/catalyseurs et deux risques maximum ;
- couverture et références.

Les détails sont bornés par symbole et globalement, mais aucun candidat ne
disparaît silencieusement de l'enveloppe. Aucun document brut n'entre au prompt.
Le brief entreprise reste indépendant du `candidate_scope_id` et réutilisable ;
chaque run Univers fige toutefois les versions réellement consommées.

Modifier :

- `trader/domain/universe/intelligence.py` : projection pure ;
- `trader/application/universe/composition.py` : champ `company_context` ;
- `trader/runtime/universe_intelligence_runtime.py` : bulk-read, couverture,
  hash et retrigger sur changement ;
- `trader/agent/universe/prompt.py` : instruction de comparer quant, situation
  news, contexte entreprise et régime sans déléguer la sélection ;
- `trader/infrastructure/state_db/universe_run_store.py` : pas de nouveau format
  requis, mais le record stocke les références.

Chaque `universe_run` persiste :

- `company_context_hash` ;
- `company_brief_refs_by_symbol` ;
- compteurs `fresh/partial/stale/missing/unsupported` ;
- symboles sélectionnés avec/sans brief ;
- mode `observe|active`.

Rollout :

1. `observe` construit, hash et trace le contexte sans modifier le prompt ;
2. vérifier couverture, tailles et sources sur un vrai scope ;
3. `active` injecte le contexte dans le prompt Univers ;
4. promouvoir `active` par défaut, avec kill switch indépendant.

Une panne micro est fail-open : l'Univers continue avec radar, news, régime et
coverage explicite. Elle ne force pas seule la baseline. La baseline reste le
fallback d'une passe Univers absente/invalide.

## 11. Mandat Univers et tranche trader

Cette tranche suit l'activation micro -> Univers ; elle ne doit pas retarder la
valeur avant sélection.

### 11.1 Contrat Univers v2

Ajouter `UniverseMandate` / `SymbolMandate` au contrat déjà dessiné dans la spec.
Pour les symboles sélectionnés seulement :

- `why_selected` ;
- `role` et `posture` ;
- `allowed_sides` éventuels, clairement distincts d'un ordre ;
- `family_context` et `company_context` bornés ;
- `company_brief_ref`, confiance et TTL.

Toujours interdits : ordre, qty, stop, sizing ou obligation de trader.

### 11.2 Prepared n'est pas active

Créer une projection `UniverseMandateStore` séparant :

- le mandat préparé avec le run exact ;
- le mandat réellement activé à T-15 ;
- un mandat fallback explicite, sans inventer de rationale agent.

À l'activation validée dans `market/rotation/venues.py`, appeler un observer
injecté par le runtime pour écrire la projection active. Ne jamais exposer au
trader un mandat seulement préparé. Un brief micro arrivé après activation ne
mute pas silencieusement le mandat actif ; il attend une nouvelle activation ou
un replan explicite.

### 11.3 Injection au trader en observe

Le seam commun est `application/decide/planner_batch.build_symbol_facts` :

- charger une fois par cycle `active_mandate_slice_for_symbol` et le brief micro
  courant ;
- passer les mappings dans `DecisionDispatchRequest` ;
- préserver la parité batch et queue ;
- injecter uniquement la tranche du symbole, jamais toute la hotlist ;
- pour un sticky hors sélection, fournir
  `managed_existing/sticky_unmandated` + contexte micro, et toujours permettre
  `REDUCE/CLOSE` ;
- persister dans `decisions.jsonl` les `mandate_ref` et
  `company_brief_refs` réellement injectés, pas une relecture après coup.

Le premier mode est `observe` : le modèle voit le mandat, mais aucune policy
déterministe ne bloque ses décisions. Une compilation `allowed_sides` en
`observe/enforce` reste un lot ultérieur après mesure ; le RiskGate reste séparé.

## 12. Observabilité

Étendre le read model et le cockpit Univers avec :

- couverture micro par venue et candidat ;
- `fresh/partial/stale/missing/unsupported/identity_mismatch` ;
- queue `pending/running/dead`, cold-start restant et prochain refresh ;
- dernier succès/échec, source, modèle et latence ;
- briefs exacts vus par chaque run Univers ;
- sélectionnés avec/sans contexte micro ;
- mandat activé et version micro vus par chaque décision.

Joindre le funnel :

```text
company source -> evidence snapshot -> company brief
    -> universe run -> activation/mandate -> decision -> outcome
```

Les références, hashes et timestamps sont nécessaires avant toute attribution
de qualité au micro-analyste.

## 13. Séquencement TDD

### Lot A — domaine et store

1. Contrats purs, bornes et fraîcheur par section.
2. Stores JSONL append-only par symbole + projection atomique + bulk-read.
3. Tests identité, source refs, stale/missing, corruption et concurrence.

### Lot B — preuves et agent micro

4. Ports de collecte et snapshot normalisé.
5. Provider yfinance fallback, puis SEC/TW/EU officiels.
6. Prompt/parser stricts et validation des sources/autorités.
7. CLI de refresh/status et backfill du scope courant.

### Lot C — runtime et agent Univers

8. Ledger/pool research dédié et scheduling événementiel.
9. Projection `UniverseCompanyContext` en `observe`.
10. Passage `active`, retrigger Univers sur nouveau hash, audit du vrai scope.
11. Ancre micro dans le brief news et provenance joinable.

### Lot D — mandat et trader

12. Contrat Univers v2 + mandat préparé/activé/fallback.
13. Injection per-symbol batch/queue en `observe`.
14. Références réellement vues dans la décision et cockpit.

Chaque lot reste committable, testable et désactivable indépendamment. Le jalon
principal demandé est la fin du lot C ; le lot D complète la continuité jusqu'au
trader sans changer l'autorité de trading.

## 14. Invariants de recette

- Tous les candidats du scope ont une entrée micro `fresh/.../missing` avant le
  prompt Univers ; seuls les détails sont bornés.
- Aucun analyste ne peut ajouter/retirer un symbole ni produire un ordre.
- L'Univers sélectionne uniquement dans le pool, au plus 25 non-sticky ; les
  sticky restent hors quota.
- Aucune donnée d'une autre entreprise ne fuit dans le payload trader d'un
  symbole.
- Aucun filing/news brut n'entre dans le prompt Univers ou trader.
- Une panne de source ou du micro-analyste est explicite et fail-open.
- Un échec Univers conserve la baseline déterministe.
- Un nouveau brief micro avant T-15 peut produire une nouvelle préparation sur
  le même scope ; après activation il ne change rien silencieusement.
- La version entreprise réellement vue est joignable jusqu'à la décision et à
  l'outcome.
- `company_thesis_status` n'est jamais présenté comme une recommandation de
  trade lorsque `security_readiness` est insuffisante.

## 15. Validation

Tests ciblés à ajouter/exécuter :

```bash
uv run pytest -q \
  tests/domain/company/test_intelligence.py \
  tests/state_db/test_company_intelligence_store.py \
  tests/application/test_company_micro_analysis.py \
  tests/test_company_micro_analyst.py \
  tests/runtime/test_company_intelligence_runtime.py \
  tests/runtime/test_news_macro_runtime.py \
  tests/domain/test_universe_intelligence.py \
  tests/application/test_universe_composition.py \
  tests/runtime/test_universe_intelligence_runtime.py \
  tests/runtime/test_decision_dispatch_runtime.py
uv run ruff check
make check
```

Tests réseau : aucun. Les smoke tests provider sont des commandes opérateur
séparées et affichent source, identité résolue, période et couverture avant tout
backfill LLM.

## 16. Hors périmètre V1

- DCF, modèle trois états, comps complets ou target price pour chaque candidat ;
- consensus exhaustif ou recommandation buy/sell ;
- retrieval historique de `situation_memory.db` ;
- policy `allowed_sides` enforce ;
- attribution de performance au micro-analyste avant que les références
  décision/outcome soient complètes ;
- scan web libre par le LLM : les sources sont collectées et normalisées côté
  code, puis le modèle synthétise un paquet borné.
