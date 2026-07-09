# Analyste-news quotidien + calendriers macro (P1-P2 de l'axe macro)

**Date** : 2026-07-02
**Status** : P1a LIVRÉ 2026-07-02 (news_items persist + macro_calendar FOMC + macro_next payload + macro_series DBnomics — testés, câblés daemon) ; P2 analyste-news RELANCÉ 2026-07-09 en architecture clean.
**Amont** : `2026-07-02-macro-fundamental-design-sketch.md` (architecture 3 étages,
phasage réordonné), `docs/superpowers/specs/2026-07-02-macro-data-sources.md` (sources),
pattern consolidateur (`trader/agent/learnings/consolidator.py`) et news-feed
(`trader/infrastructure/market_sources/news_feed.py`).

> Mise à jour 2026-07-09 : ne pas créer `trader/news_analyst.py`. L'agent
> analyste est découpé en couches :
> - domaine pur : `trader/domain/situation/`
> - cas d'usage : `trader/application/analyst/`
> - stockage : `trader/infrastructure/state_db/situation_brief_store.py`
> - adaptateur LLM : `trader/agent/news_macro/`
>
> Le brief est la source de situation court terme. Le RAG/causalité n'est pas
> un prérequis de P2 : il vient après, en index dérivé et auditable des briefs,
> news_items, décisions et outcomes.

## 1. Périmètre

P1 (calendriers-faits) + P2 (analyste-news quotidien) uniquement. Rapports/
fondamentaux (P4) et `ask_analyst` (P5) hors périmètre. Principe directeur
(Erwan) : UN agent lit le flux une fois et distribue une synthèse bornée —
le flux brut n'entre jamais dans un contexte de décision.

## 2. P1 — calendriers macro (faits code, zéro LLM)

### 2.1 Collecteur `trader/macro_calendar.py`

- **FOMC** : dates des réunions 2026 publiées un an à l'avance —
  V1 : constante versionnée dans le module (8 dates connues) + TODO scraping
  trimestriel de federalreserve.gov/monetarypolicy/fomccalendars.htm
  (structure HTML stable). Le scraping N'EST PAS dans le chemin du daemon :
  script offline qui met à jour un JSON (`state/macro_calendar.json`).
- **CPI/NFP US** : dates BLS (bls.gov/schedule/2026/home.htm) — même
  mécanique : script offline → JSON, le daemon ne fait que LIRE le JSON.
- **BCE** : dates du Conseil des gouverneurs (pages HTML BCE) — idem.
- Format du JSON : `[{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}, ...]`,
  append/écrasé par le script, versionnable dans le temps.

### 2.2 Exposition P1 : payload d'attribution d'abord

`macro_next` calculé par le code au moment de la décision (comme
`earnings_in_h`) : les 3 prochains événements avec `in_h`. Logged dans le
payload news/macro de chaque décision. PAS encore au cockpit (promotion
sur mesure, comme le veto earnings).

## 3. P2 — l'analyste-news quotidien

### 3.1 Prérequis : persister les items de news (manque actuel)

Le payload actuel ne stocke QUE des compteurs (`news_count`,
`news_coverage`) — les titres/résumés ne sont persistés nulle part.
Extension du collecteur (`news_feed.py`) : à chaque fetch réussi, appendre
les items nouveaux dans `state/news_items/YYYY-MM-DD.jsonl` :
`{fetched_at, symbol, title, publisher, published_at, link, uuid}` —
dédup par uuid par jour. Hygiène habituelle : append-only, un fichier/jour,
purge >60 j (même mécanique que radar_cache).

### 3.2 Le job analyste : cas d'usage `application/analyst`

- **Déclenchement** : pattern consolidateur — évalué en fin de cycle daemon,
  exécute si (a) dernier brief > 20 h OU (b) jamais de brief aujourd'hui et
  heure locale > 07:00. Best-effort : un échec LLM ne touche pas au cycle
  (backoff comme `ConsolidationStatusStore`).
- **Entrée** : les items du jour (+ veille si premier run) depuis
  `state/news_items/`, groupés par famille (via `family_for_symbol`) +
  le `macro_next` courant.
- **LLM** : adaptateur `trader/agent/news_macro/`, routeur existant
  (`llm.build_default_router_from_env`, profil consolidateur — spark avec
  fallback), prompt de distillation avec contrat JSON strict (pattern de
  parsing robuste du consolidateur, mais sans dépendre du module learnings).
- **Sortie** : `state/news_briefs/YYYY-MM-DD.json` :

```json
{
  "as_of": "2026-07-03T07:10:00Z",
  "valid_until": "2026-07-04T07:00:00Z",
  "zones": {"US": [{"point": "...", "sources": ["uuid1"]}],
             "EU": [...], "TW": [...]},
  "families": {"semis": [{"point": "...", "symbols": ["2330.TW"], "sources": [...]}]},
  "alerts": [{"point": "...", "severity": "info|watch", "symbols": [...]}]
}
```

  Bornes : ≤5 points par zone, ≤3 par famille, chaque point ≤200 chars,
  chaque point CITE ses uuids sources (auditables vers news_items). Toute
  version remplacée est historisée (append `news_briefs-history.jsonl` —
  leçon du chantier learnings : rien ne s'écrase sans trace).

### 3.2.1 Contrat clean/hexagonal

- `domain/situation` définit `SituationPoint` et `NewsMacroBrief` : types purs,
  normalisation, bornes, aucune I/O, aucun LLM.
- `application/analyst/news_macro.py` orchestre : construire une requête,
  appeler un port `NewsMacroAnalyst`, écrire via un port `NewsMacroBriefRepository`.
- `infrastructure/state_db/situation_brief_store.py` écrit/lit
  `state/news_briefs/YYYY-MM-DD.json` et historise les remplacements.
- `agent/news_macro/` est un adaptateur sortant : prompt, extraction JSON,
  appel LLM. Il ne choisit pas quand tourner et ne touche pas au disque.
- Pas de framework agent externe en P2 : l'agent analyste est un port
  applicatif + un adaptateur LLM via le `LlmRouter` existant (ACPX en transport
  principal aujourd'hui, remplaçable demain).

### 3.2.2 Sources et outils autorisés

L'analyste macro/news est un **compresseur de situation temporelle**. Il ne
navigue pas librement, ne choisit pas l'univers, ne trade pas et ne gère pas le
portefeuille. Il transforme des artefacts locaux, datés et sourcés en brief.

Sources P2 autorisées, en lecture seule :

| Source | Chemin / producteur | Usage par l'analyste |
|---|---|---|
| **News brutes** | `state/news_items/YYYY-MM-DD.jsonl` produit par `infrastructure/market_sources/news_feed` | Détecter changements de narration, événements idiosyncratiques, signaux faibles/forts. Fenêtre V1 : aujourd'hui + veille si premier run. |
| **Calendrier macro** | `state/macro_calendar.json` + `macro_next` calculé par `market/macro_calendar` | Situer les risques proches : FOMC, CPI/NFP, BCE, etc. |
| **Séries macro** | `state/macro_series/*.jsonl` produit par `infrastructure/market_sources/macro_series` | Lire le dernier niveau connu et, quand historique suffisant, une variation/direction. P2 peut commencer avec snapshot simple. |
| **Scope symbole/famille** | `candidate_symbols` fourni par le cas d'usage + `family_for_symbol` | Grouper les news par famille/symbole sans décider du scope de trading. Le scope vient de la rotation/radar/sticky, pas de l'analyste. |
| **Futur fondamentaux** | futurs artefacts `state/fundamental_items/`, filings, earnings, guidance briefs | Distinguer macro/famille vs idiosyncratique entreprise. Hors P2 strict, mais le contrat de brief prévoit déjà `symbols`. |

Anti-circularité hotlist :

Le radar ne doit pas être le **seul gate informationnel**. Sinon une news ou un
changement macro qui précède le prix peut être manqué parce que le symbole n'est
pas encore dans la shortlist quant. Mais l'analyste ne doit pas non plus faire
une analyse profonde de tout le pool. On découpe donc en trois anneaux :

1. **Anneau large — situation globale** : l'analyste lit macro, calendrier,
   séries et narrations par zone/famille. Pas de zoom entreprise exhaustif.
2. **Anneau event/news scout — pool observable** : le code lit les news déjà
   collectées sur un périmètre large, calcule la fraîcheur (`new_uuid_since_last_brief`,
   `last_published_at`, `item_count_24h/72h`) et remonte des
   `event_candidates`. C'est du filtrage peu coûteux, pas une étude société.
3. **Anneau profond — candidats bornés** : l'analyse symbolique/fondamentale est
   limitée à `candidate_symbols = radar_candidates ∪ event_candidates ∪ sticky
   ∪ positions/plans/watches ∪ macro_family_representatives`.

L'agent univers consomme ensuite `baseline quant + event candidates + brief
analyste + portefeuille` pour décider du mandat/hotlist finale. Donc l'analyste
peut faire entrer un symbole dans la conversation via un événement frais, mais
il ne modifie jamais directement `universe.yaml`.

Règle de fraîcheur entreprise :

- si un symbole n'a **aucune news nouvelle** depuis le dernier brief, on réutilise
  son résumé précédent jusqu'à expiration TTL ; pas de nouvel appel LLM profond ;
- si un symbole a des news nouvelles mais faibles, il peut rester en watch/context
  sans entrer dans la hotlist ;
- si un symbole hors shortlist a une news forte, l'analyste émet un
  `out_of_scope_alert`, qui devient un input du prochain passage univers.

Outils côté code :

- `NewsItemsReader` cible : lit les JSONL par fenêtre temporelle, déduplique par
  `uuid`, garde `fetched_at`, `published_at`, `symbol`, `publisher`, `link`.
- `MacroContextReader` cible : charge `macro_next` + derniers points
  `macro_series`.
- `CandidateScopeReader` cible : lit `venue_state`, `rotation_ledger`, sticky,
  positions/plans/watches, news freshness et représentants de familles macro,
  puis fournit `candidate_symbols` borné avec une provenance par symbole
  (`radar|event|sticky|position|watch|macro_family`).
- `family_for_symbol` : enrichissement déterministe famille, aucun LLM.
- `NewsMacroAnalyst` : port applicatif unique ; l'adaptateur LLM reçoit un JSON
  borné et doit rendre un `NewsMacroBrief`.
- `NewsMacroBriefStore` : persistance et historisation.

Outils explicitement interdits en P2 runtime :

- navigation web libre / scraping ad hoc depuis le prompt ;
- accès direct au broker, sizing, risk gate ou décisions d'ordre ;
- modification de `universe.yaml` ou de la hotlist ;
- appel à `recall_learnings` depuis l'analyste macro/news. Les learnings de
  trading peuvent être joints plus tard par l'univers/trader, pas par le
  compresseur de news.

ACPX workflows : autorisés seulement comme **runner offline/replay** ou
adaptateur expérimental derrière `NewsMacroAnalyst`. Le chemin prod garde le
cas d'usage Python testable.

### 3.3 Exposition P2 : attribution-first

Le daemon logge dans chaque décision `brief_ref = {"date", "as_of"}` (le
brief actif au moment de la décision) — PAS le contenu. La jointure
décision×brief×outcome devient possible dès le premier jour. AUCUNE
exposition au LLM runtime en P2.

### 3.4 P3 (rappel, hors périmètre) : `get_macro_brief{zone?|family?}` au
registre d'outils, servant le brief du jour depuis le store — après mesure.

## 4. Invariants

- Le daemon ne fait AUCUN appel réseau macro dans le cycle : il lit des
  JSON locaux produits offline (calendriers) ou déclenche l'analyste en
  best-effort hors du chemin de décision.
- L'analyste ne voit que les items collectés (pas de navigation libre).
- Briefs bornés, datés, sourcés (uuids), historisés.
- Chaque étage mesurable avant promotion (attribution-first strict).
- Causalité : un brief émet des hypothèses de situation, pas des vérités
  causales. Toute hypothèse causalement réutilisable doit garder ses sources et
  les outcomes qui la confirment/contredisent.

## 5. Tests clés (pour le plan)

- Collecteur items : dédup uuid, un fichier/jour, purge.
- macro_next : calcul in_h correct, événements passés exclus, JSON manquant
  → payload vide sans erreur.
- Analyste : déclenchement (20 h/07:00), échec LLM → backoff sans impact
  cycle, parsing strict avec réparation, bornes du brief appliquées,
  historisation de la version remplacée.
- brief_ref loggé par décision ; absent si aucun brief du jour.
- store de brief : write/read, historisation, `brief_ref`.
- contrat de domaine : borne des sections, nettoyage des points, rejet des
  payloads sans `as_of` / `valid_until`.

## 6. Décisions prises / à confirmer en début d'implémentation

- Prises : refetch quotidien REJETÉ au profit de la persistance des items
  (historique intraday conservé, auditable) ; l'analyste utilise le routeur
  consolidateur existant ; POML non utilisé en P2 (prompt f-string pattern
  consolidateur) — réévaluer en P4 pour les 10-K.
- À confirmer : le seuil de purge news_items (60 j proposé) ; l'heure du
  brief (07:00 locale = avant l'ouverture EU, après la clôture TW).


## 7. Réordonnancement final (Erwan 02/07 soir)

**P1a — COLLECTE, immédiat** : (1) persistance des items news (le manque
identifié §3.1) ; (2) calendrier macro : dates FOMC/CPI/BCE connues →
state/macro_calendar.json + `macro_next` loggé par décision ; (3) séries
macro quotidiennes via DBnomics (zéro clé API, un connecteur pour
FRED/BCE/Eurostat, pattern _post_json sans dépendance) → append-only,
déclenchement best-effort quotidien par le daemon (pattern consolidateur).
**P2 — analyste : différé au stock** (~2-3 semaines d'items).
Le scraping des calendriers (Fed/BLS/BCE) reste offline et optionnel en V1 —
les dates 2026 sont connues et versionnées.

## 8. RAG / causalité — phase post-P2

Le RAG n'est pas le premier livrable de l'analyste macro. Le premier livrable
doit produire un historique de briefs propre, sourcé et jointable aux décisions.
Ensuite seulement, on peut créer un index dérivé.

Décision 2026-07-09 : ne pas introduire de nouvelle lib RAG/agent. Le repo a
déjà le pattern `learnings.db` : SQLite WAL, FTS5, embeddings pré-calculés,
recherche hybride, scoring par outcome, traces d'injection. On réutilise ce
pattern technique, mais on garde une frontière métier stricte :

- `learnings.db` reste la mémoire de trading : ce que nos décisions/fills ont
  appris.
- `situation_memory.db` (nom cible) devient la mémoire de situation :
  macro/news/régime/entreprise, dérivée des briefs et reliée aux outcomes.
- Les archives canoniques restent les JSON/JSONL (`news_items`, `news_briefs`,
  décisions, fills). La base SQLite est reconstructible, jamais source de vérité
  unique.
- Pas de factorisation prématurée : on ne crée un module RAG générique qu'après
  deux implémentations vivantes (`learnings` + `situation`) avec des besoins
  réellement communs.

Design cible :

- **Corpus** : `news_items`, `news_briefs`, `decisions`, `fills/outcomes`,
  familles/régimes, et plus tard fondamentaux/earnings/filings.
- **Index V1** : SQLite FTS ou table dérivée proche de `learnings.db`, avec des
  lignes `situation_note` sourcées (`brief_ref`, `source_uuids`, zone, famille,
  symboles, horizon, signal weak/strong).
- **Causalité** : stocker des `causal_hypotheses`, pas des règles. Chaque ligne
  porte `hypothesis`, `support_count`, `contradict_count`, `source_refs`,
  `outcome_refs`, `last_seen`, `confidence`.
- **Consommation** : l'univers peut utiliser ces hypothèses pour expliquer la
  hotlist ; le trader ne les reçoit que comme contexte borné et attribuable.
- **Garde-fou** : aucune hypothèse ne modifie l'allocation ou le risque sans
  passage par une mesure d'attribution explicite.
