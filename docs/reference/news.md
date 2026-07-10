# Référence — News, challengers et analyste macro/news

> **Type** : Reference (Diátaxis).
> **Code** : `infrastructure/market_sources/news_feed`,
> `domain/universe/news_challengers`, `runtime/news_challenger_runtime`,
> `runtime/news_macro_runtime`, `runtime/universe_intelligence_runtime`,
> `agent/news_macro`, `agent/universe`.
> **État** : archive, scout, analyste, injection du brief, préparation univers et
> observabilité par run implémentés ; retrieval historique non activé.

## Propriété des décisions

Les rôles ne se confondent jamais :

| Composant | Produit | Ne décide jamais |
|---|---|---|
| Archive news | observations brutes datées | attribution à un émetteur, shortlist, hotlist |
| Scout fresh-news | challengers qualifiés et sourcés | hotlist |
| Analyste macro/news | brief de situation par venue/famille/symbole | ajout/retrait de symbole, hotlist |
| Agent univers | hotlist de 25 non-sticky maximum | ordres, sizing, admission risk |
| Composition déterministe | univers actif = hotlist choisie + sticky | choix stratégique des 25 |

Flux cible canonique :

```text
clôture venue -> radar top 40
  -> parent quantitatif immuable (`scope_phase=close`)

pré-open T-90 -> radar top 40 + scout fresh-news, y compris overnight
  -> enfant final immuable (`scope_phase=preopen`, parent close tracé)

pool candidat + news/macro
  -> analyste macro/news async
  -> brief sourcé portant le même `candidate_scope_id`

pool candidat + brief + régime disponible + sticky de contexte
  + GlobalFamilyBoard comparatif non allocateur
  -> agent univers async
  -> projection préparée pour ce scope exact

pré-open T-15 -> active la projection ou la baseline avec fallback observable
  -> hotlist choisie (<= 25 non-sticky)

hotlist choisie + sticky
  -> composition déterministe
  -> univers actif
```

Dans le runtime nominal, l'agent univers rend une `selected_hotlist` complète.
Le code dérive ensuite des `add/remove` contre la baseline uniquement pour la
compatibilité du ledger. La baseline n'est que la référence backtestable et le
fallback explicite. Le brief courant est déjà projeté dans le prompt ; seul le
retrieval de situations historiques reste `not_enabled`.

## 1. Archive canonique — `state/news_items`

`news_feed` collecte Yahoo via yfinance et append les observations dans :

```text
state/news_items/YYYY-MM-DD.jsonl
```

Chaque ligne garde notamment `uuid`, `fetched_at`, `published_at`, `symbol`,
`title`, `publisher` et `link`. L'archive est dédupliquée par UUID et purgée
après 60 jours.

Le champ historique `symbol` signifie en pratique **symbole interrogé**
(`fetched_for`), pas attribution certaine de l'article. Une ligne fetchée pour
TSMC peut parler d'ASML ; aucun consommateur ne doit donc lui faire confiance
sans attribution par le titre.

La couverture est actuellement **partielle** : l'archive est alimentée quand le
cycle enregistre une décision et demande son snapshot news. Un symbole non
interrogé ne doit jamais être interprété comme `no_news`.

Il n'existe pas encore de source news globale indépendante garantie. L'analyste
lit `state/macro_headlines/` ou `state/global_news_items/` seulement si un
producteur local les a déposés ; sinon la couverture globale vaut explicitement
`missing`. De même, les dates macro peuvent provenir des constantes versionnées
et du fichier calendrier local de fallback. Ces limites apparaissent dans la
couverture du brief.

## 2. Scout fresh-news — construction des challengers

Le scout est un filtre déterministe local, sans LLM et sans navigation web :

- fenêtre de fraîcheur maximale : 72 h ;
- attribution directe depuis le titre via noms légaux, alias et ticker qualifié ;
- rejet des ambiguïtés, mauvais échanges et simples citations d'analystes ;
- seuil de matérialité/scoring V1 : 65 ;
- un symbole doit rester présent dans le ranking radar éligible complet : la
  news peut contourner le top 40, jamais les critères data/liquidité/exclusion ;
- aucun quota global de challengers ;
- rétention jusqu'à `fresh_news.valid_until`, puis sortie à expiration ou perte
  d'éligibilité.

Le pool candidat devient :

```text
top 40 radar ∪ tous les challengers fresh-news qualifiés
```

Chaque challenger figure dans le snapshot `state/venue_state.json` avec son vrai
`attractiveness`, son `bias`, son score news, son TTL et au plus trois preuves
compactes. Le run qui l'a produit est conservé dans
`state/news_challenger_runs/*.jsonl`, puis repris dans le scope immuable
`state/candidate_scopes/*.jsonl`.

## 3. Analyste macro/news — digestion de la situation

Après la création du scope final pré-open, `NewsMacroAnalysisRunner` soumet un job
asynchrone single-flight. Il ignore explicitement le parent `scope_phase=close` :
les news publiées dans la soirée sont donc disponibles avant de produire le brief.
Le daemon continue son cycle ; un second job ne peut pas se superposer et le mode
`--once` ne lance pas le thread.

L'analyste reçoit, par venue :

- tout le pool candidat, sans troncature arbitraire du nombre de symboles ;
- les news entreprise correspondantes, dont l'évidence attribuée des challengers ;
- les headlines macro/globales locales disponibles ;
- le calendrier et les derniers points de séries macro ;
- le contexte famille déterministe.

Le fusible de 80 porte sur le **nombre d'articles injectés au prompt**, pas sur
le nombre de candidats. La sortie est un brief borné, daté et sourcé :

```text
state/news_briefs/YYYY-MM-DD.jsonl       # archive canonique append-only
state/news_briefs/latest-<venue>.jsonl   # cache reconstructible
```

Le contrat exige des noms lisibles dans `sources` et des UUID/références stables
dans `source_refs`. L'enveloppe de références d'entrée est forcée côté code ;
chaque `source_ref` de sortie est filtrée contre le catalogue d'entrée, les noms
sont redérivés de ce catalogue et les points sans référence valide sont retirés.
Le brief trace les nombres de points retenus et rejetés. L'analyste ne
renvoie aucun `add/remove` et ne modifie jamais le pool, la hotlist ou
`universe.yaml`.

Un brief garde un TTL de 20 h, sans geler les inputs pendant 20 h : une signature
matérielle différente (nouvelles UUID, série ou événement macro) provoque un
refresh après un cooldown de succès de 4 h. Le champ mobile `in_h` ne compte pas
comme nouvelle information. Le runner coalesce le dernier trigger reçu pendant
un appel LLM.

`input_refs` porte aussi le `candidate_scope_id` exact et une couverture
explicite : nombre de candidats, candidats avec news, articles injectés, cap
atteint ou non, disponibilité des headlines globales, séries macro présentes ou
stales. `status=partial` signifie exactement « corpus local observé », jamais
« scan exhaustif sans résultat ».

## 4. Agent univers — seul composeur de la hotlist

L'agent univers arbitre les 25 non-sticky à partir du pool candidat et de son
contexte. Le scout propose des candidats ; l'analyste décrit la situation ;
**seul l'agent univers sélectionne la hotlist**.

État runtime actuel :

- au pré-open, le prompt univers reçoit les candidats du scope final, l'évidence
  `fresh_news`, les sticky de contexte, le régime disponible et une projection
  bornée du brief courant ;
- le brief n'est accepté que si son `candidate_scope_id` correspond exactement au
  scope courant ; les zones/familles/symboles/alertes utiles sont projetés, pas
  les `input_refs` bruts ;
- le runner est async, single-worker et coalesce le dernier trigger pendant un
  run ; il ne bloque jamais le polling du daemon ;
- une réussite écrit une projection préparée indexée par le hash du scope ;
- au pré-open, la sortie est revalidée contre pool, sticky, cap et TTL ;
- en cas d'absence, mismatch, invalidité, expiration ou erreur agent, la baseline
  déterministe est conservée avec un `fallback_reason` explicite ;
- un fallback `pending`/`missing` reste une tentative : il est dédupliqué dans le
  ledger, mais une projection arrivée plus tard dans la même fenêtre peut encore
  être activée ; seule une activation agent réussie clôt le scope ;
- tous les sticky sont ajoutés **après** la sélection et ne consomment aucune des
  25 places.

## 5. Observabilité du funnel challenger → hotlist

### Artefacts implémentés

- news brutes : `state/news_items/*.jsonl` ;
- briefs : `state/news_briefs/*.jsonl` ;
- run scout : `state/news_challenger_runs/*.jsonl` + cache latest par venue ;
- scopes immuables : `state/candidate_scopes/*.jsonl` + projection
  `current-<venue>.json`, avec `scope_phase`, `parent_candidate_scope_id` et
  `parent_close_at` ;
- runs agent : `state/universe_runs/*.jsonl` + cache latest par venue ;
- sélection préparée exacte :
  `state/universe_prepared/<sha256(candidate_scope_id)>.json` ;
- activation pré-open, baseline vs hotlist effective et fallback :
  `state/rotation_ledger.jsonl` et `state/venue_state.json` ;
- référence du brief actif sur les décisions ;
- index FTS dérivé : `state/situation_memory.db` ;
- projection opérateur `universe_pipeline` dans le read model et panneau cockpit
  par venue, séparant résultat agent et activation/fallback effectif.
- board famille cross-venue append-only :
  `state/global_family_boards/*.jsonl` + `current.json`, référencé par chaque run
  agent et affiché comme `context only` dans le cockpit.

### Deux ledgers de décision, deux propriétaires

Ne pas fusionner le run du scout et la décision de l'agent univers :

```text
state/news_challenger_runs/YYYY-MM-DD.jsonl
state/universe_runs/YYYY-MM-DD.jsonl
```

Un run scout garde les métriques amont. Exemple abrégé :

```json
{
  "schema_version": 1,
  "candidate_run_id": "20260710T200000.000000Z:US:<uuid>",
  "as_of": "2026-07-10T20:00:00Z",
  "venue": "US",
  "coverage_status": "partial",
  "eligible_symbol_count": 245,
  "radar_symbol_count": 40,
  "archived_items_read": 1703,
  "items_read": 1703,
  "eligible_items": 4,
  "rejection_counts": {
    "stale": 400,
    "no_direct_entity": 300,
    "analyst_source_only": 12,
    "low_score": 20
  },
  "challengers": [
    {
      "symbol": "DE",
      "score": 70,
      "valid_until": "2026-07-13T10:00:00Z",
      "publishers": ["Reuters"],
      "source_refs": ["news-uuid"]
    }
  ],
  "status": "success",
  "reason": "selection_complete"
}
```

Le `candidate_run_id` est attaché au scope même lorsque le scout ne retient aucun
challenger ou termine en mode degraded/error : la couverture amont reste donc
joignable et l'absence de candidat n'efface pas le run.

Le run univers référence ensuite son scope exact et porte seul la sélection :

```json
{
  "agent_run_id": "universe:US:2026-07-11T13:20:00+00:00:<id>",
  "candidate_scope_id": "candidate_scope:v1:US:<sha256>",
  "candidate_run_ids": ["20260710T200000.000000Z:US:<uuid>"],
  "brief_ref": {"venue": "US", "brief_id": "..."},
  "baseline": ["AAPL", "MSFT"],
  "sticky_context": ["NVDA"],
  "selected_hotlist": ["AAPL", "DE"],
  "selected_challengers": ["DE"],
  "status": "success",
  "fallback_used": false,
  "retrieval_status": "not_enabled"
}
```

Le run agent porte la décision préparée ; le ledger de rotation porte
l'activation et les sticky effectivement composés. Les refus item par item ne
sont pas recopiés : les compteurs agrégés suffisent, avec les `source_refs`
complets uniquement pour les challengers retenus. Les ledgers décision/fill
permettent ensuite de relier sélection, usage et outcome.

## 6. RAG, FLAIR et MemRL

### Vérité actuelle

- Les news brutes ne sont **pas** « dans un RAG » : elles sont dans le JSONL
  canonique `news_items`.
- Les points des briefs sont copiés dans `situation_memory.db`, index SQLite
  FTS5 dérivé et reconstructible.
- `SituationMemoryStore.search()` existe, mais aucun agent runtime ne l'appelle
  encore : ce n'est donc pas un RAG actif de bout en bout.
- Le RAG `learnings.db` est une pile différente. Le trader l'interroge via
  `recall_learnings` lorsqu'une analogie historique est utile; aucune expérience
  n'est poussée automatiquement dans le cockpit.
- **FLAIR** (pas FLARE) pondère les learnings selon leurs outcomes. Dans la
  mémoire de situation, `outcome_score` reste actuellement neutre.
- MemRL est actif sur cette pile **learnings** : les recalls sont reliés aux
  outcomes différés et `q_value` contribue au ranking avec shrinkage.
- MemRL n'est pas encore actif sur `situation_memory.db` ni chez l'agent Univers.

### Décision

Le chemin court `scout -> pool candidat -> agent univers -> hotlist` n'a pas
besoin de RAG. Il doit rester déterministe, frais et auditable.

Si un retrieval de situation est activé, son premier consommateur est **l'agent
univers** : il pourra demander des situations historiques analogues aux
candidats/briefs actuels avant de composer la hotlist. Le RAG fournit du contexte,
il ne choisit aucun symbole.

FLAIR et MemRL ne remplacent pas le retrieval :

- retrieval/FTS répond à « quelles situations passées sont pertinentes ? » ;
- dans la future mémoire de situation, FLAIR répondra à « lesquelles ont été
  confirmées par les outcomes ? » ;
- pour la future mémoire de situation, MemRL répondra à « quels rappels ont
  réellement aidé les décisions de l'agent univers ? » une fois ses injections
  et récompenses tracées. Ce chemin est distinct du MemRL learnings déjà actif.

Un challenger ne doit pas être dupliqué directement comme document RAG. Son
évaluation va dans le ledger d'observabilité et référence les UUID des news. S'il
devient un point du brief, il entre déjà dans `situation_memory.db` via le brief.

Ordre de promotion restant :

1. exploiter les jointures déjà persistées entre scope, runs, brief et rotation ;
2. relier ensuite ces références aux décisions et outcomes dans les read models ;
3. mesurer les besoins de rappel et la qualité FTS ;
4. seulement ensuite brancher la recherche sur l'agent univers ;
5. activer FLAIR situation, puis MemRL, quand il existe assez d'outcomes et de
   traces de retrieval pour les évaluer.

## Voir aussi

- [Gestion d'univers](universe-rotation.md)
- [Architecture de connaissance](agent-knowledge-architecture.md)
- [Learnings & RAG](learnings-rag.md)
- [Données macro](macro.md)
- [Spec analyste macro/news](../superpowers/specs/2026-07-02-macro-analyste-news-spec.md)
- [Spec agent univers](../superpowers/specs/2026-07-09-universe-intelligence-pass-design.md)
