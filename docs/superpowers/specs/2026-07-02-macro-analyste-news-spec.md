# Analyste-news quotidien + calendriers macro (P1-P2 de l'axe macro)

**Date** : 2026-07-02
**Statut** : ✅ **P1-P2 LIVRÉS — SPEC FIGÉE (2026-07-10).** P1a persiste les
news, expose le calendrier et collecte les séries macro. P2 produit les briefs
async par venue, les lie au `candidate_scope_id` exact et les injecte de façon
bornée à l'agent univers. Les fondamentaux, l'outil interactif et le retrieval de
situations historiques restent hors périmètre de cette spec.

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
> un prérequis de P2 : l'index dérivé existe déjà, mais aucun agent runtime ne le
> consulte encore. S'il est activé, son premier consommateur sera l'agent univers.
>
> Mise à jour 2026-07-10 : la tranche runtime de l'analyste reçoit désormais,
> en plus des news symboles et du calendrier, les derniers points
> `state/macro_series/*.jsonl`, un `family_context` déterministe via
> `family_for_symbol`, et des headlines globales locales si un producteur dépose
> `state/macro_headlines/YYYY-MM-DD.jsonl` ou
> `state/global_news_items/YYYY-MM-DD.jsonl`. Il ne fait toujours pas de
> navigation web libre. Il n'existe pas encore de producteur global indépendant
> garanti : l'absence de ces fichiers est donc enregistrée comme couverture
> manquante. Les news symboles viennent du corpus local partiel et le payload LLM
> est borné par venue (`80` articles max, pas 80 candidats) pour éviter les
> timeouts et préserver un digest quotidien fiable.
>
> Mise à jour cadence 2026-07-10 : le runner ignore le parent quantitatif de
> clôture et attend l'enfant final créé à T-90 du pré-open. Le brief couvre donc
> les challengers et news overnight connus avant l'ouverture, puis l'agent univers
> consomme exactement ce même `candidate_scope_id`.

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

### 3.1 Prérequis livré : persister les items de news

Le payload historique ne stockait que des compteurs (`news_count`,
`news_coverage`). Le collecteur (`news_feed.py`) append désormais à chaque fetch
réussi les items nouveaux dans `state/news_items/YYYY-MM-DD.jsonl` :
`{fetched_at, symbol, title, publisher, published_at, link, uuid}` —
dédup par uuid par jour. Hygiène habituelle : append-only, un fichier/jour,
purge >60 j (même mécanique que radar_cache).

### 3.2 Le job analyste : cas d'usage `application/analyst`

- **Déclenchement** : runner asynchrone single-worker évalué après le cycle dans
  le daemon. Il tente un run lorsqu'aucun brief actif ne correspond au
  `candidate_scope_id` courant ou quand les inputs matériels ont changé depuis
  au moins 4 h ; un brief produit reste valide 20 h et une signature identique
  impose un backoff de 6 h après échec. `macro_next.in_h` est exclu de la
  signature afin de ne pas créer de faux refreshs.
  Best-effort : un échec LLM ne touche pas au cycle. Le daemon soumet le job puis
  continue immédiatement ; un trigger reçu en vol remplace le pending précédent
  et sera rejoué après le run courant, sans concurrence. Le
  mode `--once` ne lance pas ce thread.
- **Entrée** : les items du jour et de la veille depuis
  `state/news_items/`, groupés par famille (via `family_for_symbol`) +
  le `macro_next` courant.
- **LLM** : adaptateur `trader/agent/news_macro/`, routeur existant
  (`llm.build_default_router_from_env`, profil consolidateur sans fallback
  secondaire propre à l'analyste), prompt de distillation avec contrat JSON
  strict (pattern de parsing robuste du consolidateur, mais sans dépendre du
  module learnings).
- **Sortie canonique** : `state/news_briefs/YYYY-MM-DD.jsonl`, append-only,
  une ligne par passage analyste et par venue (`TW`, `EU`, `US`, éventuellement
  `GLOBAL`). Les fichiers `latest-<venue>.jsonl` sont des caches pratiques
  d'une ligne JSONL,
  reconstruisibles depuis le JSONL, jamais la source de vérité.

```json
{
  "brief_id": "2026-07-03T07:10:00Z|EU",
  "venue": "EU",
  "as_of": "2026-07-03T07:10:00Z",
  "valid_until": "2026-07-04T07:00:00Z",
  "input_refs": {
    "candidate_scope_id": "candidate_scope:v1:EU:<sha256>",
    "news_item_uuids": ["uuid1"],
    "coverage": {
      "status": "partial",
      "candidate_count": 43,
      "candidates_with_news": 12,
      "global_headlines_status": "missing"
    }
  },
  "zones": {"US": [{"point": "...", "sources": ["Reuters"],
                      "source_refs": ["uuid1"]}],
             "EU": [...], "TW": [...]},
  "families": {"semis": [{"point": "...", "symbols": ["2330.TW"],
                              "sources": ["Bloomberg"], "source_refs": [...]}]},
  "alerts": [{"point": "...", "severity": "info|watch", "symbols": [...]}]
}
```

  Bornes : ≤5 points par zone, ≤3 par famille, chaque point ≤200 chars,
  chaque point sépare les noms lisibles (`sources`) des références stables
  (`source_refs`, UUID news ou référence locale macro). Un UUID ne doit jamais
  être présenté comme nom de source. L'enveloppe `input_refs` est forcée côté
  code ; l'appartenance de chaque `source_ref` au catalogue est revalidée, les
  noms sont dérivés du catalogue et les points sans source valide sont retirés
  avec compteurs de validation. Aucun brief
  n'est écrasé : une nouvelle analyse ajoute une ligne JSONL avec un nouveau
  `brief_id`. Leçon du chantier learnings : on conserve les décisions et les
  observations datées, puis on reconstruit les index/caches.

### 3.2.1 Contrat clean/hexagonal

- `domain/situation` définit `SituationPoint` et `NewsMacroBrief` : types purs,
  normalisation, bornes, aucune I/O, aucun LLM.
- `application/analyst/news_macro.py` orchestre : construire une requête,
  appeler un port `NewsMacroAnalyst`, écrire via un port `NewsMacroBriefRepository`.
- `infrastructure/state_db/situation_brief_store.py` écrit/lit
  `state/news_briefs/YYYY-MM-DD.jsonl` en append-only et maintient seulement des
  caches `latest-<venue>.jsonl` reconstructibles.
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
| **News brutes** | `state/news_items/YYYY-MM-DD.jsonl` produit par `infrastructure/market_sources/news_feed` | Détecter changements de narration, événements idiosyncratiques, signaux faibles/forts. Fenêtre V1 : aujourd'hui + veille à chaque run. |
| **Headlines macro/globales locales** | `state/macro_headlines/YYYY-MM-DD.jsonl` ou `state/global_news_items/YYYY-MM-DD.jsonl`, producteur offline optionnel non garanti | Donner les narrations géopolitiques, taux, énergie, trade war, secteur ou zone sans autoriser l'agent à naviguer librement ; couverture `missing` en l'absence de fichier. |
| **Calendrier macro** | `state/macro_calendar.json` + `macro_next` calculé par `market/macro_calendar` | Situer les risques proches : FOMC, CPI/NFP, BCE, etc. |
| **Séries macro** | `state/macro_series/*.jsonl` produit par `infrastructure/market_sources/macro_series` | Lire le dernier niveau connu et, quand historique suffisant, une variation/direction. P2 peut commencer avec snapshot simple. |
| **Scope symbole/famille** | shortlist candidate fournie en amont : top 40 radar + tous les challengers fresh-news qualifiés, puis `family_for_symbol` | Analyser les news entreprise seulement sur cette shortlist et les grouper par famille. L'analyste ne construit ni la shortlist ni la hotlist. |
| **Futur fondamentaux** | futurs artefacts `state/fundamental_items/`, filings, earnings, guidance briefs | Distinguer macro/famille vs idiosyncratique entreprise. Hors P2 strict, mais le contrat de brief prévoit déjà `symbols`. |

### 3.2.3 Distinction canonique : shortlist candidate vs hotlist

- **Shortlist candidate** : les 40 premiers du radar, complétés par **tous**
  les challengers fresh-news qualifiés hors radar. Elle peut donc dépasser 40 ;
  il n'existe pas de troncature arbitraire du nombre de candidats.
- **Hotlist choisie** : au plus 25 symboles non-sticky choisis plus tard par
  l'agent univers à partir de la shortlist, du brief macro/news courant et des
  autres contextes autorisés. Le brief est projeté de façon bornée et n'est
  accepté que s'il référence exactement le même `candidate_scope_id`.
- **Univers final** : la hotlist choisie + tous les sticky. Les sticky sont hors
  quota : ils ne consomment aucune des 25 places et peuvent porter le total
  final au-delà de 25.

L'anti-circularité concerne la **construction de la shortlist** : elle ne doit
pas dépendre de la hotlist finale qu'elle sert ensuite à produire. Ce problème
n'est pas résolu en demandant à l'analyste de scanner toutes les entreprises.
L'analyste reçoit la shortlist déjà construite, analyse la macro globalement et les
news entreprise uniquement pour ces candidats. Il ne l'élargit pas, ne choisit
pas la hotlist et ne modifie jamais `universe.yaml`.

La couverture fresh-news est explicitement **partielle** : les challengers sont
détectés uniquement dans les headlines effectivement collectées dans
`news_items`, avec attribution directe et sourcée. Ce n'est ni un scan de toutes
les sociétés ni une couverture exhaustive du marché. Un symbole sans headline
collectée et attribuable ne peut pas devenir challenger dans cette passe.

Outils côté code :

- `NewsItemsReader` cible : lit les JSONL par fenêtre temporelle, déduplique par
  `uuid`, garde `fetched_at`, `published_at`, `symbol`, `publisher`, `link`.
- `MacroContextReader` cible : charge `macro_next` + derniers points
  `macro_series`.
- `GlobalHeadlinesReader` cible : lit des JSONL locaux `macro_headlines` /
  `global_news_items`, filtre par `regions|venues|zones`, déduplique par `uuid`
  et transmet seulement un payload compact.
- `CandidateScopeReader` : lit toute la shortlist candidate produite en amont
  (`venue_state.candidates` = top 40 radar + tous les challengers) et son
  `candidate_scope_id`, puis la fournit sans cap de candidats. Pour un
  challenger, il transmet l'évidence `fresh_news.evidence`, force le symbole
  attribué du record candidat, déduplique par UUID et conserve un fusible sur le
  volume total de news envoyé au brief.
- `family_for_symbol` : enrichissement déterministe famille, aucun LLM.
- `NewsMacroAnalyst` : port applicatif unique ; l'adaptateur LLM reçoit un JSON
  borné et doit rendre un `NewsMacroBrief`.
- `NewsMacroBriefStore` : persistance append-only, lecture active par venue et
  caches latest reconstructibles.

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

### 3.3 Exposition P2 : attribution et injection directe à l'univers

Lorsqu'un brief actif existe pour la venue, sa référence est conservée sans
recopier le contenu. Le runner d'univers exige que
`brief.input_refs.candidate_scope_id` corresponde au scope courant, projette
seulement les zones, familles, symboles et alertes utiles, puis prépare la
sélection hors de la boucle synchrone. Le pré-open active uniquement la projection
préparée pour ce scope exact. Un brief absent ou décalé produit un statut
observable (`brief_missing` ou `brief_scope_mismatch`) et la baseline
déterministe est utilisée ; le daemon n'attend jamais le LLM.

Le brief n'est pas injecté directement au trader symbole dans P2. Le futur mandat
univers pourra lui en transmettre une tranche utile, sans corpus brut.

### 3.4 P3 (rappel, hors périmètre) : `get_macro_brief{zone?|family?}` au
registre d'outils, servant le brief du jour depuis le store — après mesure.

## 4. Invariants

- Le daemon ne fait AUCUN appel réseau macro dans le cycle : il lit des
  JSON locaux produits offline (calendriers) ou déclenche l'analyste en
  best-effort hors du chemin de décision.
- Le runner est activé par défaut et peut être coupé avec
  `CASYS_NEWS_MACRO_ANALYST_ENABLED=0`. Il ne backfill pas un état live ancien :
  un daemon actif et un scope matérialisé à la clôture sont nécessaires.
- L'analyste ne voit que les items collectés (pas de navigation libre).
- Briefs bornés, datés, sourcés (`sources` lisibles + `source_refs` stables),
  conservés en JSONL append-only.
- Les bases SQLite/RAG et les caches `latest` sont des dérivés
  reconstructibles, jamais les archives canoniques.
- Chaque étage mesurable avant promotion (attribution-first strict).
- Causalité : un brief émet des hypothèses de situation, pas des vérités
  causales. Toute hypothèse causalement réutilisable doit garder ses sources et
  les outcomes qui la confirment/contredisent.

## 5. Tests clés (pour le plan)

- Collecteur items : dédup uuid, un fichier/jour, purge.
- macro_next : calcul in_h correct, événements passés exclus, JSON manquant
  → payload vide sans erreur.
- Analyste : absence de brief actif, single-flight, signature identique → backoff
  6 h, échec LLM sans impact cycle, parsing strict avec réparation, bornes du brief appliquées,
  append d'une nouvelle ligne JSONL sans écraser les briefs précédents.
- Couplage de scope : le brief garde le `candidate_scope_id` exact ; l'agent
  univers attend si le brief manque ou appartient à un autre scope.
- Préparation : la projection bornée ne contient pas les `input_refs` bruts et
  l'activation pré-open ne lit qu'une projection préparée pour le même scope.
- brief_ref loggé par décision ; absent si aucun brief actif pour la venue.
- store de brief : append JSONL, cache latest par venue, lecture active,
  `brief_ref`, corruption fail-safe.
- contrat de domaine : borne des sections, nettoyage des points, rejet des
  payloads sans `as_of` / `valid_until`.

## 6. Décisions prises

- Prises : refetch quotidien REJETÉ au profit de la persistance des items
  (historique intraday conservé, auditable) ; l'analyste utilise le routeur
  consolidateur existant ; POML non utilisé en P2 (prompt f-string pattern
  consolidateur) — réévaluer en P4 pour les 10-K.
- Acté au runtime : purge `news_items` après 60 jours ; pas d'heure fixe 07:00.
  Le runner analyse un nouveau scope ou des inputs matériels modifiés après un
  cooldown de succès de 4 h, avec validité 20 h et backoff 6 h sur échec de
  signature identique.


## 7. Réordonnancement historique (Erwan 02/07 soir)

**P1a — COLLECTE, immédiat** : (1) persistance des items news (le manque
identifié §3.1) ; (2) calendrier macro : dates FOMC/CPI/BCE connues →
state/macro_calendar.json + `macro_next` loggé par décision ; (3) séries
macro quotidiennes via DBnomics (zéro clé API, un connecteur pour
FRED/BCE/Eurostat, pattern _post_json sans dépendance) → append-only,
déclenchement best-effort quotidien par le daemon (pattern consolidateur).
**P2 — analyste : différé au stock** (~2-3 semaines d'items). Cette attente est
désormais écoulée : les fondations P2 ont été livrées le 2026-07-10.
Le scraping des calendriers (Fed/BLS/BCE) reste offline et optionnel en V1 —
les dates 2026 sont connues et versionnées.

Cette spec est désormais figée pour P1-P2. La suite active — mandat enrichi,
injection au trader, policy observe/enforce et éventuel retrieval historique —
est suivie dans
[`2026-07-09-universe-intelligence-pass-design.md`](2026-07-09-universe-intelligence-pass-design.md).

## 8. Index de situation dérivé / RAG futur

Décision 2026-07-10 : le chemin frais n'attend pas un RAG :

```text
scout -> shortlist candidate -> brief courant -> agent univers -> hotlist
```

L'index historique est un enrichissement possible de l'agent univers, jamais un
producteur de shortlist ou de hotlist.

### 8.1 Vérité actuelle

- `news_items/*.jsonl` contient les observations brutes canoniques. Les news ne
  sont pas stockées « dans un RAG ».
- `news_briefs/*.jsonl` contient les briefs canoniques append-only.
- `situation_memory.db` existe comme index SQLite WAL + FTS5 reconstructible des
  points de briefs. `SituationMemoryStore.search()` existe, mais aucun agent
  runtime ne l'appelle encore : ce n'est pas un RAG actif de bout en bout.
- L'ingestion de situation démarre avec `outcome_score=0.0`, `q_value=None` et
  sans embeddings exploités. Il n'y a donc ni FLAIR situation effectif ni MemRL.
  (MAJ 2026-08-16 : FLAIR situation livré depuis 9fa7920 — voir
  docs/reference/situation-memory.md.)
- `learnings.db` est une pile métier différente et déjà active : recall hybride
  exposé au trader symbole, avec embeddings et reranking FLAIR par outcome.

### 8.2 Consommateur cible et propriété

Si la mémoire de situation est activée, son premier consommateur est **l'agent
univers**. Le retrieval répond à « quelles situations historiques ressemblent au
brief et aux candidats actuels ? ». Il fournit quelques analogues datés,
sourcés et TTLés avant que l'agent univers compose lui-même les 25 non-sticky.

Le scout, l'analyste, le RAG, FLAIR et MemRL ne peuvent émettre ni `add/remove`,
ni shortlist, ni hotlist :

- le scout construit le pool frais ;
- l'analyste décrit la situation présente ;
- le retrieval rappelle des situations passées pertinentes ;
- FLAIR pondère plus tard celles confirmées par les outcomes ;
- MemRL pourra apprendre quels recalls ont aidé une décision, seulement après
  instrumentation des recalls et rewards ;
- l'agent univers arbitre et sélectionne.

### 8.3 Persistance et observabilité

On ne duplique pas les challengers dans un nouveau corpus : le ledger scout
référence leurs UUID de news. Si l'analyste transforme l'événement en point de
brief, ce point est déjà indexé dans `situation_memory.db`.

Les archives canoniques restent les JSONL et ledgers (`news_items`,
`news_briefs`, runs scout/univers, décisions, fills). La base SQLite reste un
dérivé reconstructible. Avant d'ajouter embeddings, FLAIR situation ou MemRL, il
faut pouvoir joindre :

```text
news item -> run scout -> brief -> recall -> run agent univers -> outcome
```

Pas de factorisation prématurée avec `learnings.db` : les deux mémoires gardent
des contrats métier distincts même si elles partagent SQLite/FTS5.
