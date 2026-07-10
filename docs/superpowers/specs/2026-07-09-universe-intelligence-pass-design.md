# Universe intelligence pass — hotlist enrichie, mandat et contexte symbole

> **Statut : 🛠 ACTIF — sélection d'univers livrée, mandat enrichi à poursuivre.**
> Depuis le 2026-07-10 : parent close top 40, enfant pré-open
> `40 radar + challengers` incluant l'overnight, briefs exact-scope,
> préparation async par venue, sélection propre de l'agent (`<=25` non-sticky),
> activation pré-open, fallback explicite, sticky hors quota et observabilité par
> run sont codés. Restent le `UniverseMandate` enrichi, sa projection au trader,
> la policy observe/enforce, le retrieval de situation optionnel et la migration
> hexa progressive. Cette spec reste donc active.
>
> S'appuie sur : **D9/D10/D13** (radar, rotation, hotlists par venue,
> override LLM pré-open), **D2** (`family_regime`), l'architecture de
> connaissance agent (situation vs compétence vs expérience), et l'axe
> analyste-news/macro.

## 1. Intention

La gestion d'univers ne doit plus seulement répondre à :

> Quels symboles méritent d'être surveillés ?

Elle doit progressivement répondre à :

> Quels symboles méritent d'être dans le scope du trader, pourquoi, avec quel
> mandat d'exposition, et quelle information entreprise/famille doit accompagner
> la décision locale ?

Aujourd'hui, l'univers live est déjà dynamique : radar daily déterministe,
rotation à hystérésis, préparation LLM async par venue, activation exacte au
pré-open, puis écriture de `config/universe.yaml`. La sélection agent produit sa
propre `selected_hotlist`, mais ne laisse pas encore de vraie fiche de mission au
trader symbole.

La proposition : transformer la passe hotlist en **passe d'intelligence
d'univers**. Elle garde la baseline déterministe et l'override borné, mais elle
produit en plus un mandat compact, traçable et consommable par l'agent de trading.

## 2. Problème observé

Le découpage actuel crée une tension :

- L'agent de trading grain-symbole décide localement, mais il ne porte pas le
  mandat explicite de constituer l'univers.
- L'agent univers compose déjà la hotlist complète et le runtime dérive encore
  `add/remove` pour compatibilité et mesure contre la baseline.
- Les news et la macro sont digérées dans des briefs directement consommés par
  l'agent univers, mais pas encore projetés sous forme de mandat au trader.
- Le régime famille existe, mais il sert surtout de contexte de lecture et non
  de mandat d'exposition.
- Le dossier `trader/market/` mélange encore domaine pur, orchestration,
  accès données, LLM override, état runtime et écriture de fichiers.

Résultat : l'agent de trading peut voir une partie du portefeuille et des plans,
mais il doit encore reconstruire implicitement :

1. pourquoi ce symbole est dans le scope ;
2. si le signal vient du symbole, de la famille, de la venue ou du marché ;
3. quelles directions sont acceptables dans le contexte du book ;
4. quelles informations entreprise/famille sont vraiment pertinentes.

## 3. Principe de responsabilité

Séparer cinq rôles :

| Rôle | Responsabilité | Ne doit pas faire |
|---|---|---|
| **Radar + scout fresh-news** | Construire le pool candidat : top 40 quantitatif + tous les challengers qualifiés. | Choisir la hotlist ou interpréter le portefeuille. |
| **Analyste-news / macro** | Digérer les événements bruts en brief borné : symbole, famille, venue, marché. | Ajouter/retirer un symbole, composer la hotlist, trader ou sizer. |
| **Agent univers / PM pass** | Composer lui-même la hotlist de 25 non-sticky maximum et produire un mandat contextuel par symbole. | Déléguer ce choix au brief/RAG, poser les stops ou exécuter les ordres. |
| **Trading agent symbole** | Décider quoi faire localement sous mandat : entry, hold, reduce, close, watch. | Refaire toute la sélection d'univers ou ignorer le mandat. |
| **Admission / risk déterministe** | Appliquer les fusibles et, plus tard, les contraintes enforceables du mandat. | Inventer une thèse ou cacher une décision stratégique. |

Formule courte :

```text
radar/scout dit quels symboles peuvent être considérés
analyste-news dit uniquement ce qui se passe
agent univers choisit lui-même ce qu'on veut regarder et sous quel mandat
trading agent dit quoi faire sur le symbole
risk/admission dit ce qui a le droit de passer
```

## 4. Architecture cible

Flux livré pour la sélection, puis cible du mandat :

```text
clôture de venue
  -> pool.yaml -> ranking radar éligible -> top 40 radar
  -> news_items + ranking éligible -> scout -> challengers qualifiés
  -> top 40 radar + challengers -> pool candidat + candidate_scope_id immuable
  -> analyste async -> brief portant ce scope exact
  -> agent univers async -> projection préparée pour ce scope exact

pré-open de la même venue
  -> activation projection préparée, ou baseline + fallback_reason
  -> selected_hotlist (<= 25 non-sticky)

selected_hotlist + sticky -> composition déterministe -> universe.yaml

cible restante : universe_mandate state -> decide_one(symbol) -> risk/admission
```

`universe.yaml` reste, en V1, le fichier simple qui pilote le scope actif. Le
mandat enrichi doit vivre dans un état séparé pour ne pas casser le runtime :

- option fichier : `state/universe_mandate.json` ou
  `state/universe_mandates/YYYY-MM-DD.json`;
- option SQLite plus tard : table `universe_mandates`, versionnée par `mandate_id`.

Le mandat doit être loggé par référence dans `decisions.jsonl`, pas recopié en
entier dans chaque décision.

Les fondations de sélection sont activées par défaut. Les opérateurs peuvent
couper séparément l'analyste (`CASYS_NEWS_MACRO_ANALYST_ENABLED=0`) et la
préparation univers (`CASYS_UNIVERSE_INTELLIGENCE_ENABLED=0`) sans bloquer la
rotation déterministe. Les nouveaux artefacts sont événementiels : sur un état
live ancien, ils n'apparaissent qu'avec un daemon actif, à la prochaine clôture
de chaque venue puis dans sa fenêtre pré-open, lors des runs async associés. Il
n'y a pas de backfill automatique du scope courant.

### 4.1 Sources d'information de la passe univers

L'agent univers ne va pas chercher le web brut en live à chaque rotation. Il
consomme des artefacts locaux, datés, bornés et sourcés. Les familles d'inputs
sont :

| Input | Source actuelle / cible | Rôle dans le mandat |
|---|---|---|
| **1. Signaux quant forts** | `state/venue_state.json` (`candidates`, `default_hotlist`, scores, bias), `state/rotation_ledger.jsonl`, radar/rotation | Dire quels symboles sont objectivement attractifs et mesurer baseline vs final. |
| **2. Régime famille / marché** | `family_regime`, snapshot typé `state/last_regime.json` | Transformer une sélection symbole en lecture famille/venue. Couverture actuelle explicitement partielle : familles de l'univers tradable actif ; snapshot stale après 96 h. |
| **3a. Évidence challenger fraîche** | `fresh_news` borné sur les candidats, produit par le scout | Expliquer pourquoi un symbole hors top 40 est devenu candidat sans exposer le corpus brut. |
| **3b. Brief news/macro courant** | `state/news_briefs/`, produit par l'analyste à partir des news, headlines globales, calendrier et séries | Décrire la situation présente par symbole, famille, zone et venue. |
| **3c. Analogues historiques, futur** | retrieval borné de `situation_memory.db`, index dérivé des briefs | Rappeler des situations passées pertinentes ; jamais proposer ou sélectionner un symbole. |
| **4. Entreprise / fondamentaux** | court terme : news/earnings par symbole ; cible : briefs fondamentaux/filings/guidance distillés par l'analyste offline | Distinguer mouvement famille vs information idiosyncratique entreprise, et identifier les exceptions. |
| **5. Portefeuille / sticky** | actuel : sticky collector (positions, plans, watches) ; cible mandat : expositions/read models portefeuille | Les sticky sont déjà visibles comme contexte puis ajoutés hors quota ; la lecture d'exposition plus riche reste à livrer avec le mandat. |

Le scout produit 3a ; l'analyste-news/macro produit 3b et les futurs briefs de 4 ;
le retrieval dérivera 3c de briefs passés. Aucun n'est composeur de la hotlist.
Le brief courant 3b est déjà projeté de façon bornée dans le prompt univers ; 3c
reste `not_enabled`. L'absence d'une source ou d'un retrieval est exposée par les
métadonnées de couverture au lieu d'être interprétée comme une absence de risque.

### 4.2 Anti-circularité entre shortlist candidate et hotlist

Deux listes distinctes, puis une composition finale, sont canoniques :

1. **Shortlist candidate** : le radar et le scout fresh-news composent le top 40
   radar complété par tous les challengers qualifiés hors radar. Elle peut
   dépasser 40 et constitue le scope donné à l'analyste pour lire les news
   entreprise.
2. **Hotlist choisie** : au plus 25 symboles non-sticky décidés ensuite par
   l'agent univers à partir de la shortlist enrichie par les briefs et les autres
   contextes autorisés.
3. **Univers final** : les 25 choisis au maximum + tous les sticky. Une position,
   un plan, une watch ou un ordre pending ne consomme jamais une place des 25.

L'anti-circularité porte sur la construction de la première : la shortlist ne
doit pas dépendre de la hotlist future. Le scout construit ce pool ; l'analyste
en produit uniquement une lecture de situation. Aucun des deux ne peut émettre
un ajout/retrait de hotlist. Le flux cible est donc :

```text
radar top 40 + tous challengers fresh-news qualifiés -> shortlist candidate
shortlist + macro/news -> briefs analyste bornés
shortlist + briefs + contexte univers -> agent univers -> 25 choisis max
25 choisis max + tous sticky hors quota -> univers final
```

Le nom historique `default_hotlist` dans certains artefacts reste un champ de
compatibilité ; il s'agit d'une baseline déterministe et du fallback en cas
d'échec agent, pas de la décision nominale. `add/remove` reste un format de
ledger et un adapter legacy explicite ; le chemin nominal refuse ce contrat et
exige `selected_hotlist`, rationales et postures famille complètes.

### 4.3 Candidate builder / scout fresh-news

La candidate shortlist V1 est construite avant l'analyste et avant l'agent
univers :

```text
candidate_scope =
  top_40_radar_candidates
  ∪ all_qualified_fresh_news_challengers
```

Le candidate builder/scout ne décide pas la hotlist finale et ne remplace pas
l'agent univers. Il produit seulement une version enrichie, datée, justifiée et mesurable du scope
candidat. Il n'applique aucun cap global après le top 40 : tout challenger qui
passe les gardes de fraîcheur, matérialité et attribution est conservé.

Snapshot de scope implémenté :

```json
{
  "schema_version": 1,
  "candidate_scope_id": "candidate_scope:v1:TW:<sha256>",
  "candidate_run_ids": ["20260709T083000Z:TW:<uuid>"],
  "as_of": "2026-07-09T08:30:00Z",
  "venue": "TW",
  "candidates": [
    {
      "symbol": "2330.TW",
      "attractiveness": 0.81,
      "bias": "long",
      "candidate_source": "fresh_news",
      "candidate_sources": ["fresh_news"],
      "fresh_news": {
        "score": 91,
        "latest_published_at": "2026-07-09T07:52:00Z",
        "event_types": ["earnings_guidance"],
        "source_refs": ["uuid-1"],
        "evidence": [{"uuid": "uuid-1", "title": "...", "publisher": "Reuters"}]
      }
    }
  ],
  "default_hotlist": ["2330.TW", "2454.TW"],
  "sticky_context_at_close": ["2317.TW"]
}
```

Sources V1 :

- ranking radar éligible complet : source des 40 premiers et garde d'éligibilité
  des challengers ;
- `state/news_items/YYYY-MM-DD.jsonl` : corpus local effectivement collecté,
  sans LLM ; sa couverture est partielle et ne vaut pas scan exhaustif du marché.
- `domain/semantic/catalog.py` : `family_for_symbol`.

Ordre de priorité :

1. top 40 radar, dans l'ordre d'attractiveness ;
2. tous les challengers fresh-news hors radar qui passent les gardes ;
3. agent univers : au plus 25 choix non-sticky dans cette shortlist ;
4. composition finale : ajout de tous les sticky hors quota.

Bornes V1 :

- `radar_limit = 40` ;
- aucun `fresh_news_max` ni `max_symbols` sur les candidats qualifiés ;
- fusible de volume sur les news/evidences injectées au brief, avec
  déduplication UUID ;
- `hotlist_cap = 25` pour les symboles choisis ;
- sticky illimités et hors quota.

Tests contractuels du candidate builder :

- fresh news : tous les symboles hors radar qualifiés entrent dans le scope ;
- no fresh news : un symbole hors radar sans nouveauté ne déclenche pas de zoom ;
- couverture partielle : l'absence de headline collectée n'est pas interprétée
  comme absence d'événement ;
- sticky : 25 choisis + tous les sticky, sans consommation du quota ;
- caps : radar limité à 40, candidats non tronqués, hotlist choisie limitée à 25 ;
- provenance : chaque symbole indique pourquoi il a été retenu.

### 4.4 Observabilité du scout et de l'agent univers — livrée

La clôture et les deux décisions différentes disposent de trois archives
append-only :

```text
state/candidate_scopes/YYYY-MM-DD.jsonl
state/news_challenger_runs/YYYY-MM-DD.jsonl
state/universe_runs/YYYY-MM-DD.jsonl
```

`candidate_scopes` fige, par venue et clôture, le pool, la baseline, les sticky
vus à la clôture, les `candidate_run_ids` et le `candidate_scope_id`. Les caches
`current-<venue>.json` sont des projections atomiques reconstructibles.

Le run scout porte un `candidate_run_id` et conserve la couverture réellement
observée (`coverage_status=partial`, compteurs de symboles, `items_read`,
`eligible_items`), les compteurs de rejet agrégés, puis les challengers retenus
avec score, TTL et `source_refs`. Les statuts `success|degraded|error` et leur
raison sont explicites. Il ne prétend jamais avoir scanné toutes les entreprises.

Le run agent porte un `agent_run_id`, le `candidate_scope_id`, les
`candidate_run_ids` et le `brief_ref`, puis conserve baseline, sticky de contexte,
hotlist choisie, challengers effectivement sélectionnés, latence, couverture et
statut. Les attentes (`brief_missing`, `brief_scope_mismatch`) et erreurs sont
également appendues. Une réussite écrit en plus une projection atomique exacte :

```text
state/universe_prepared/<sha256(candidate_scope_id)>.json
```

Le pré-open ne lit que cette projection ; il revalide pool, sticky, cap et TTL.
`rotation_ledger.jsonl` enregistre ensuite l'activation, le `fallback_used`, son
`fallback_reason`, l'`agent_run_id`, le `brief_ref` et les deltas contre la
baseline. Les UUID complets ne sont gardés que pour les événements retenus ; les
rejets amont restent agrégés pour éviter de recopier le corpus brut.

Ces identifiants permettent ensuite les jointures vers décisions, fills et
outcomes sans confondre :

```text
news item -> run scout -> brief -> run agent univers -> décision -> fill/outcome
```

### 4.5 Mémoire de situation, RAG, FLAIR et MemRL

Le chemin court `scout -> shortlist candidate -> agent univers -> hotlist` ne
requiert aucun RAG. Il dépend d'abord de faits frais, bornés et auditables.

État réel au 2026-07-10 :

- `news_items/*.jsonl` est l'archive brute canonique, pas un RAG ;
- `news_briefs/*.jsonl` est l'archive canonique des situations digérées ;
- `situation_memory.db` est un index FTS5 dérivé de ces briefs ; sa recherche
  n'a encore aucun consommateur runtime ;
- `learnings.db` est une mémoire de trading séparée, dont le recall est déjà
  exposé à l'agent symbole ;
- FLAIR reranke aujourd'hui les learnings par outcome ; les outcomes de la
  mémoire de situation sont encore neutres ;
- MemRL n'est pas actif : le champ `q_value` existe sans updater ni ranking.

Si le retrieval de situation est activé, son premier consommateur est l'agent
univers. Il reçoit des analogues historiques sourcés en plus du brief présent,
mais reste seul décideur de la hotlist. FLAIR pourra ensuite pondérer les
situations confirmées ; MemRL ne devient utile qu'après avoir enregistré quels
recalls ont effectivement aidé quelles décisions.

Un challenger n'est donc pas dupliqué dans un nouveau RAG : son run référence les
UUID de news. S'il devient un point du brief, ce point est déjà indexé dans la
mémoire de situation dérivée.

## 5. Contrat de mandat proposé — encore actif

V1 contractuelle, volontairement plus riche que `add/remove`, mais encore bornée :

```json
{
  "mandate_id": "2026-07-09:TW:preopen",
  "agent_run_id": "2026-07-09:TW:preopen",
  "candidate_run_id": "2026-07-09T00:20:00Z:TW",
  "as_of": "2026-07-09T00:30:00Z",
  "valid_until": "2026-07-10T00:30:00Z",
  "venue": "TW",
  "brief_refs": ["news_brief:2026-07-09:TW:brief_id"],
  "baseline": {
    "default_hotlist": ["2330.TW", "2454.TW"],
    "source": "radar_hysteresis"
  },
  "selected_hotlist": ["2330.TW", "2454.TW"],
  "sticky_context": ["2317.TW"],
  "sticky_added_after_selection": ["2317.TW"],
  "fallback_used": false,
  "portfolio_posture": {
    "gross_mode": "normal|cautious|risk_off",
    "net_bias": "long|short|neutral",
    "notes": ["TW semis risk-off, short chase capped after multi-day move"]
  },
  "symbols": [
    {
      "symbol": "2330.TW",
      "family": "semis_tw",
      "role": "core_candidate|hedge_candidate|watch_only|reduce_only",
      "posture": "active|watch_only|reduce_only|blocked",
      "allowed_sides": ["long", "short"],
      "confidence": 0.72,
      "ttl": "1 trading day",
      "why_selected": [
        "radar top-ranked",
        "family move is strong",
        "liquid proxy for TW semis"
      ],
      "family_context": {
        "direction": "down",
        "strength": "high",
        "summary": "semis_tw under pressure, broad weakness"
      },
      "company_context": {
        "summary": "no major idiosyncratic negative news detected",
        "event_risk": "none_known|earnings_soon|guidance|regulatory|other",
        "source_refs": ["news_brief:2026-07-09:TW:brief_id:semis_tw"]
      },
      "portfolio_context": {
        "exposure_note": "avoid increasing net long TW semis",
        "risk_notes": ["rebound risk after multi-day selloff"]
      }
    }
  ],
  "rejects": [
    {
      "symbol": "XYZ",
      "reason": "redundant_with_selected_symbol|weak_signal|bad_liquidity"
    }
  ]
}
```

Règles :

- Pas de flux news brut dans ce contrat.
- Chaque résumé doit être court, daté, TTLé et sourcé par référence.
- `role/posture/allowed_sides` sont des instructions de mandat, pas des ordres.
- `confidence` mesure la qualité du mandat univers, pas la probabilité du trade.
- La baseline déterministe reste visible pour mesurer l'alpha de l'override.
- `selected_hotlist` est la sélection propre de l'agent et contient au plus 25
  non-sticky ; `sticky_added_after_selection` est composé ensuite.
- Le runtime actuel peut encore sérialiser cette décision en `add/remove` contre
  la baseline. C'est un encodage de compatibilité, pas un transfert de propriété
  au code déterministe.

## 6. Ce que reçoit le trader symbole

Le trader symbole ne reçoit pas toute la hotlist. Il reçoit la tranche qui le
concerne :

```text
Universe mandate:
- role: hedge_candidate
- posture: active
- allowed_sides: short only
- family_context: semis_tw down, broad weakness
- company_context: no major idiosyncratic news detected
- portfolio_context: current book already short TW semis; do not chase if rebound confirmed
- ttl: 1 trading day

Trading task:
Manage 2330.TW under this mandate. Use local indicators and current position/plan.
Prefer HOLD/REDUCE/CLOSE if the local setup conflicts with the mandate.
```

Le trader garde son autonomie locale : il peut refuser un trade si le timing est
mauvais, poser une watch, réduire une position ou fermer. Mais il n'a plus à
réinventer pourquoi le symbole est dans le scope.

## 7. Lien avec l'analyste-news et la mémoire de situation

Cette spec ne remplace pas l'analyste-news. Elle le consomme.

L'analyste-news transforme :

```text
items bruts, earnings, headlines macro, événements
```

en :

```text
brief sourcé par symbole/famille/venue/marché
```

L'analyste ne sélectionne aucun symbole et ne recommande aucun ajout/retrait. La
passe agent univers transforme ensuite :

```text
shortlist candidate + brief + régime + contexte portefeuille
```

en :

```text
hotlist choisie (<= 25 non-sticky) + mandat + contexte symbole
```

Puis la composition déterministe ajoute les sticky hors quota. Si le retrieval
de `situation_memory.db` est activé, il enrichit l'entrée de l'agent univers avec
des situations historiques analogues ; il ne remplace ni le brief courant ni la
décision de l'agent.

Le runner analyste, la persistance des briefs et le raccord direct au prompt
univers existent. Le brief n'est jamais pris « au plus récent » sans contrôle :
son `input_refs.candidate_scope_id` doit être celui du scope préparé. Sa projection
contient uniquement les zones, familles, symboles candidats, alertes, sources et
métadonnées de couverture utiles ; les `input_refs` bruts n'entrent pas au prompt.

La couverture reste honnêtement partielle : les news symboles proviennent du
corpus local observé, il n'existe pas de source globale indépendante garantie,
et le calendrier peut reposer sur les constantes/fichiers locaux de fallback.
Ces limites sont des métadonnées du brief, pas des affirmations `no_news`.
Le code revalide en plus les références contre le catalogue d'entrée et retire
les points sans source valide. Le snapshot famille indique `observed`,
`no_evidence`, `truncated_or_no_evidence` ou `not_reported`, et une posture est
obligatoire pour toute famille effectivement sélectionnée.

## 8. Lien avec allocation/risk

Le mandat univers peut d'abord être **informatif** :

- posture injectée au trader ;
- log dans `decisions.jsonl` ;
- dashboard d'audit.

Le log ne copie pas le contenu complet des briefs : il persiste des références
(`brief_ref`, `mandate_ref`) vers les JSONL canoniques, pour préserver
l'historique et permettre les jointures attribution/outcome.

Puis il peut devenir enforceable via le futur split `observe|enforce` :

- `watch_only` bloque les nouvelles entrées ;
- `reduce_only` autorise `CLOSE/REDUCE`, bloque `OPEN/SCALE_IN` ;
- `allowed_sides=["short"]` bloque les nouveaux longs ;
- `blocked` autorise seulement les sorties de risque ;
- les contraintes portefeuille restent appliquées par une couche déterministe,
  pas par prompt.

Important : `RiskGate` doit rester le fusible final. La politique de mandat
devrait vivre dans une couche applicative/domaine dédiée, par exemple
`PortfolioMandateGate` ou `UniverseMandatePolicy`, avant le `RiskGate`.

## 9. Cible clean / hexagonale

Le chantier est aussi une occasion de dégonfler progressivement `trader/market/`.
Ne pas faire un big-bang. Utiliser un strangler pattern.

Découpage cible :

| Couche | Responsabilité cible |
|---|---|
| `trader/domain/universe/` | Hotlist, posture, mandat, sticky, hystérésis, contrats purs. |
| `trader/domain/portfolio/` | Exposition, caps, net/gross/side/family exposure, replay. |
| `trader/domain/market/` | Régime pur, indicateurs purs, labels de marché. |
| `trader/application/universe/` | Orchestration de la passe univers, validation/compilation des sorties LLM, écriture de projections applicatives. |
| `trader/application/decide/` | Décision trading symbole sous contexte/mandat. |
| `trader/infrastructure/market_sources/` | Yahoo, news, macro, providers, cache, fetch. |
| `trader/infrastructure/llm/` ou `trader/agent/` | Backends LLM, prompts concrets, parsing de réponses agent. |
| `trader/runtime/` | Wiring daemon, schedule, side effects, fail-safe. |
| `trader/reporting/` | Read models et dashboards du mandat/hotlist. |

Invariant architectural :

- Le domaine ne lit pas de fichiers, n'appelle pas le LLM, ne fetch pas Yahoo.
- L'application orchestre des ports/protocols.
- L'infrastructure fournit les adapters.
- Le runtime branche et protège.

## 10. Audit restant avant le mandat enrichi

Conserver un audit séparé par axe avant d'implémenter le mandat enrichi :

1. **Axe architecture**
   - Cartographier `trader/market/rotation/*`.
   - Classer chaque fonction en domain/application/infrastructure/runtime.
   - Proposer le plan de migration sans big-bang.

2. **Axe contrats runtime**
   - Lister les consommateurs de `config/universe.yaml`.
   - Lister les producteurs/lecteurs de `state/venue_state.json`,
     `state/rotation_ledger.jsonl`, `state/events.jsonl`.
   - Vérifier le chemin queue : `universe -> due -> decidable -> quiet_gate -> LLM`.

3. **Axe agents**
   - Comparer agent rotation actuel, analyste-news, trader symbole.
   - Définir les contrats JSON exacts et les prompts.
   - Vérifier les chemins fail-safe si le LLM d'univers échoue.

4. **Axe observabilité/backtest**
   - Définir comment mesurer baseline vs final.
   - Définir un replay de la semaine du 2026-07-06 au 2026-07-09.
   - Mesurer ce que le mandat aurait signalé avant/après le retournement short.

5. **Axe tests**
   - Tests purs du contrat mandat.
   - Tests d'intégration sans LLM avec fake override.
   - Tests de compat `add/remove` historique.
   - Tests sticky : une position/plan ouvert est ajouté hors quota à l'univers final.

## 11. Phasage proposé

### Phase 0 — audit et socle de sélection (livré)

Livrables :

- cartographie des responsabilités actuelles ;
- issue parent `Universe intelligence pass`;
- issues enfants : contrat, analyste-news dependency, context injection,
  policy observe/enforce, clean migration.

Le socle runtime a été livré au-delà de l'audit initial : scopes immuables,
runners async, sélection complète, activation exacte et observabilité.

### Phase 1 — contrat et projection en observe

Ajouter un générateur de `universe_mandate` avec fake/LLM injectable, mais ne pas
l'injecter au trader.

Livrables :

- modèle pur `UniverseMandate`;
- renderer/read model d'audit ;
- écriture `state/universe_mandate.json`;
- lien ledger `rotation_ledger` vers `mandate_id`.

### Phase 2 — injection au trader symbole

Injecter seulement la tranche du mandat liée au symbole dans les facts de
`decide_one`.

Mode `observe` :

- le trader voit le mandat ;
- rien n'est bloqué par ce mandat ;
- on mesure s'il le respecte spontanément.

### Phase 3 — policy déterministe observe/enforce

Compiler les champs `posture/allowed_sides` en verdicts déterministes.

Mode initial :

- `observe`: warnings seulement ;
- `enforce`: blocage des nouvelles ouvertures contraires au mandat, sorties de
  risque toujours autorisées.

### Phase 4 — analyste-news livré ; mémoire historique optionnelle

Le raccord du brief courant et ses `source_refs`/TTL est livré. Reste à mesurer
avant d'activer le retrieval :

- formalisation `catalysts forward` / `narrative backward` dans le mandat ;
- traces de recall et attribution au-delà des `candidate_run_id` / `agent_run_id`
  déjà persistés ;
- retrieval FTS de situations analogues par l'agent univers, seulement si les
  métriques montrent un bénéfice.

### Phase 5 — migration clean architecture

Déplacer progressivement les fonctions pures vers `domain/universe`, puis
réduire `trader/market/rotation` à des adapters/facades jusqu'à extinction.

## 12. Non-objectifs

- Ne pas remplacer l'agent de trading par un agent portefeuille unique.
- Ne pas laisser le LLM d'univers exécuter ou sizer des ordres.
- Ne pas injecter de news brutes dans les prompts de trading.
- Ne pas casser `universe.yaml` ni le chemin historique `add/remove`.
- Ne pas déplacer tout `trader/market/` en une seule PR.
- Ne pas rendre les mandats enforceables avant d'avoir un mode observe mesuré.

## 13. Critères d'acceptation globaux — partiellement ouverts

- [x] La baseline déterministe continue de produire la hotlist de fallback
  quand l'intelligence pass est désactivée ou échoue.
- [x] `add/remove` reste dérivable pour le ledger et disponible dans l'adapter
  legacy, mais est refusé comme sortie du chemin nominal.
- [x] En chemin nominal, l'agent univers est le seul propriétaire décisionnel de la
  hotlist ; la baseline déterministe est la référence et le fallback.
- [ ] Un mandat enrichi est produit, daté, TTLé, sourcé, et traçable par
  `mandate_id`, `candidate_scope_id` et `agent_run_id`.
- [ ] Le trader symbole peut recevoir une tranche de mandat sans voir toute la
  hotlist ni les sources brutes.
- [x] La hotlist contient au plus 25 choisis ; tous les sticky restent protégés et
  sont ajoutés hors quota.
- [ ] Les décisions peuvent être reliées au mandat actif dans le ledger.
- [x] Un échec du LLM d'univers conserve le comportement déterministe actuel.
- [x] L'absence de RAG de situation ne bloque ni le scout ni la composition ; un
  échec de retrieval conserve le brief courant et le chemin nominal.
- [ ] Les tests du mandat enrichi distinguent bien :
    - sélection d'univers ;
    - contexte/mandat ;
    - décision symbole ;
    - admission/risk.

## 14. Décision de cadence et questions ouvertes

**Cadence tranchée** : pour chaque venue `TW`, `EU`, `US`, la clôture fige un
parent quantitatif ; T-90 crée l'enfant final news-aware et déclenche les deux
passes async ; T-15 active la projection exacte. Il n'existe pas de décision
cross-venue unique. Le `GlobalFamilyBoard` d'observation comparative TW/EU/US est
livré : les trois agents le consomment, mais il n'est volontairement pas un
allocateur de capital.

1. Quelle rétention pour les ledgers scout/univers ? Les rejets scout restent
   agrégés ; seuls les candidats retenus gardent leurs références détaillées.
2. Quelle granularité pour `allowed_sides` : symbole seulement, famille, venue,
   ou portefeuille global ?
3. Quel TTL par défaut pour un mandat : une session, une journée, jusqu'à
   prochaine rotation, ou dépendant du signal ?
4. Quand le brief signale une situation entreprise contraire au régime famille,
   l'agent univers arbitre. Quels champs de trace rendent cet arbitrage lisible ?
5. La surface cockpit minimale `scope → scout → brief → agent → activation` est
   livrée ; restent les postures détaillées, raisons de rejet et timeline du
   mandat enrichi.
6. Le `GlobalFamilyBoard` append-only est livré avec rangs intra-venue, couverture,
   fraîcheur, observations bornées et doctrine `context not allocation` ; une
   allocation portefeuille cross-venue reste explicitement hors de son mandat.

## 15. Issue map suggérée

- **Parent** : `Universe intelligence pass : hotlist enrichie, mandat et contexte symbole`.
- **Child A** : audit hexa de `trader/market/rotation` et plan de migration.
- **Child B** : contrat `UniverseMandate` + projection observe.
- **Child C** : injection du mandat dans `decide_one` sans enforcement.
- **Child D** : compilation `posture/allowed_sides` vers policy observe/enforce.
- **Child E (raccord/observabilité livrés)** : évaluer le retrieval de situation
  par l'agent univers et tracer son attribution avant activation.
- **Child F** : dashboard/read model `universe_mandate`.
