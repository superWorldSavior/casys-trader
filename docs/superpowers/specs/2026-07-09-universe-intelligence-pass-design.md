# Universe intelligence pass — hotlist enrichie, mandat et contexte symbole

> **Statut : 🧭 DESIGN — principe validé en discussion avec Erwan (2026-07-09).**
> Pas de code de production dans cette spec. Prochaine étape recommandée :
> audit architecture + contrats avant implémentation.
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
rotation à hystérésis, override LLM parcimonieux en pré-open, puis écriture de
`config/universe.yaml`. C'est un bon socle. Mais l'override LLM ne produit que
`add/remove`, donc il ne laisse pas de vraie fiche de mission au trader symbole.

La proposition : transformer la passe hotlist en **passe d'intelligence
d'univers**. Elle garde la baseline déterministe et l'override borné, mais elle
produit en plus un mandat compact, traçable et consommable par l'agent de trading.

## 2. Problème observé

Le découpage actuel crée une tension :

- L'agent de trading grain-symbole décide localement, mais il ne porte pas le
  mandat explicite de constituer l'univers.
- L'agent de rotation existe déjà, mais il ne renvoie qu'une correction de
  hotlist (`add/remove`).
- Les news et la macro sont collectées/loggées, mais elles ne sont pas encore
  digérées en contexte live pour le trader.
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

Séparer quatre rôles :

| Rôle | Responsabilité | Ne doit pas faire |
|---|---|---|
| **Analyste-news / macro** | Digérer les événements bruts en situation bornée : symbole, famille, venue, marché. | Trader, choisir les tailles, lire le book en temps réel. |
| **Universe / PM pass** | Choisir le scope de trading et produire un mandat d'exposition/contextuel par symbole. | Poser les stops ou exécuter les ordres. |
| **Trading agent symbole** | Décider quoi faire localement sous mandat : entry, hold, reduce, close, watch. | Refaire toute la sélection d'univers ou ignorer le mandat. |
| **Admission / risk déterministe** | Appliquer les fusibles et, plus tard, les contraintes enforceables du mandat. | Inventer une thèse ou cacher une décision stratégique. |

Formule courte :

```text
analyste-news dit ce qui se passe
universe/PM pass dit ce qu'on veut regarder et sous quel mandat
trading agent dit quoi faire sur le symbole
risk/admission dit ce qui a le droit de passer
```

## 4. Architecture cible

Flux cible :

```text
pool.yaml
  -> radar daily déterministe
  -> rotation/hystérésis par venue
  -> analyste-news/macro + régime + portefeuille + sticky
  -> universe intelligence pass
  -> universe.yaml             (liste active, compat actuelle)
  -> universe_mandate state    (mandat enrichi, nouveau)
  -> decide_one(symbol)        (contexte symbole + mandat)
  -> risk/admission            (observe puis enforce)
```

`universe.yaml` reste, en V1, le fichier simple qui pilote le scope actif. Le
mandat enrichi doit vivre dans un état séparé pour ne pas casser le runtime :

- option fichier : `state/universe_mandate.json` ou
  `state/universe_mandates/YYYY-MM-DD.json`;
- option SQLite plus tard : table `universe_mandates`, versionnée par `mandate_id`.

Le mandat doit être loggé par référence dans `decisions.jsonl`, pas recopié en
entier dans chaque décision.

### 4.1 Sources d'information de la passe univers

L'agent univers ne va pas chercher le web brut en live à chaque rotation. Il
consomme des artefacts locaux, datés, bornés et sourcés. Les cinq familles
d'inputs sont :

| Input | Source actuelle / cible | Rôle dans le mandat |
|---|---|---|
| **1. Signaux quant forts** | `state/venue_state.json` (`candidates`, `default_hotlist`, scores, bias), `state/rotation_ledger.jsonl`, radar/rotation | Dire quels symboles sont objectivement attractifs et mesurer baseline vs final. |
| **2. Régime famille / marché** | `family_regime`, futur `state/last_regime.json` | Transformer une sélection symbole en lecture famille/venue : risk-on, risk-off, dispersion, concentration. |
| **3. News / signaux faibles** | `state/news_items/YYYY-MM-DD.jsonl` en brut collecté ; cible `state/news_briefs/` ou `situation_state` produit par l'analyste-news | Détecter les changements de narration, flux, sentiment, signaux faibles et signaux forts événementiels. |
| **4. Entreprise / fondamentaux** | court terme : news/earnings par symbole ; cible : briefs fondamentaux/filings/guidance distillés par l'analyste offline | Distinguer mouvement famille vs information idiosyncratique entreprise, et identifier les exceptions. |
| **5. Portefeuille / sticky** | `state/current_report.json`, `state/casys.db`, plans ouverts, positions, watches, sticky collector | Adapter la hotlist au book réel : exposition long/short, concentration, positions à gérer, symboles non retirables. |

L'analyste-news/macro est donc requis pour la version complète des inputs 3 et
4, mais la passe univers peut démarrer en **observe** avant cela avec les
artefacts déjà disponibles : radar, régime, portefeuille, sticky, compteurs news
et `company_context: not_available` explicite.

### 4.2 Anti-circularité entre analyste et hotlist

L'agent analyste ne doit pas analyser profondément toutes les entreprises du
pool pour produire la hotlist : ce serait coûteux, peu borné, et cela
remplacerait la rotation. Mais le radar ne doit pas être le seul gate
informationnel : une news ou un changement macro peut précéder le prix. Le flux
cible est :

1. **Radar/rotation** produit une baseline déterministe et backtestable :
   candidates, `default_hotlist`, scores, venue/family bias.
2. **Analyste macro/news** produit une situation large zones/familles sans zoom
   entreprise exhaustif.
3. **Event/news scout** lit la fraîcheur des news sur le pool observable :
   nouveaux uuids, `last_published_at`, compte 24h/72h, signal fort/faible. Il
   ajoute des `event_candidates` sans étude profonde.
4. **Zoom profond** seulement sur un scope borné :
   radar candidates + event candidates + sticky + positions/plans/watches +
   représentants de familles macro touchées.
5. **Agent univers** décide le mandat final en combinant baseline quant,
   situation analyste, portefeuille et règles sticky.
6. **Trader** reçoit le mandat/scope final et le contexte symbole utile.

Exception contrôlée : si l'analyste voit une news forte sur un symbole hors
scope déjà collecté par `news_items`, il peut produire un `out_of_scope_alert`.
Cette alerte ne modifie pas directement `universe.yaml` ; elle devient un input
du prochain passage univers.

Cette règle reprend le point de l'issue #1 : le signal d'opportunité ne doit pas
être calculé sur un univers déjà capé, sinon on introduit une circularité. La
macro/famille et la fraîcheur des news servent donc à élargir le scope
d'analyse, mais la hotlist finale reste décidée par l'agent univers.

### 4.3 Prochaine tranche codable : enrichir la candidate shortlist existante

On a déjà une candidate shortlist : D10/radar écrit `venue_state.candidates` et
`default_hotlist` avec un cap courant autour de 50 symboles. La prochaine tranche
ne recrée pas cette liste. Elle l'enrichit avec des déclencheurs non-price
cheap, une provenance explicite et un fusible global avant le passage agent
univers.

```text
candidate_scope =
  existing_radar_candidates
  ∪ sticky_symbols
  ∪ fresh_news_symbols
  ∪ macro_family_representatives
  ∪ exploration_slots
```

Ce composant ne décide pas la hotlist finale et ne remplace pas la rotation. Il
produit seulement une version **enrichie** du scope candidat, bornée, datée,
justifiée et mesurable.

Contrat cible :

```json
{
  "as_of": "2026-07-09T08:30:00Z",
  "max_symbols": 80,
  "symbols": [
    {
      "symbol": "2330.TW",
      "family": "semis_tw",
      "sources": ["radar", "fresh_news", "macro_family"],
      "fresh_news": {
        "new_uuid_since_last_brief": 3,
        "last_published_at": "2026-07-09T07:52:00Z",
        "item_count_24h": 4,
        "item_count_72h": 7
      },
      "radar": {"rank": 4, "score": 0.81},
      "sticky": false
    }
  ]
}
```

Sources V1 :

- `state/venue_state.json` : `candidates`, `default_hotlist`, scores/ranks.
- `state/news_items/YYYY-MM-DD.jsonl` : fraîcheur news par symbole, sans LLM.
- sticky runtime : positions ouvertes, plans, watches.
- `state/news_briefs/YYYY-MM-DD.json` : familles/zones macro touchées, quand
  disponible.
- `domain/semantic/catalog.py` : `family_for_symbol`.

Ordre de priorité :

1. sticky : jamais perdu ;
2. candidates radar existants : baseline quant déjà produite par D10 ;
3. fresh news : symboles avec nouveaux uuids depuis le dernier brief ;
4. macro family reps : quelques représentants des familles touchées ;
5. exploration slots : quota explicite pour éviter l'angle mort.

Bornes V1 proposées :

- reprendre le cap radar existant (`cap_m`, aujourd'hui ~50) ;
- `fresh_news_max = 20` ;
- `macro_family_reps_per_family = 3` ;
- `exploration_max = 5` ;
- `max_symbols = 80` fusible global.

Tests à écrire avant branchement agent :

- fresh news : un symbole hors radar avec nouvel uuid entre dans le scope ;
- no fresh news : un symbole hors radar sans nouveauté ne déclenche pas de zoom ;
- sticky : position/plan/watch reste dans le scope même hors radar ;
- cap : priorité stable et `max_symbols` respecté ;
- provenance : chaque symbole indique pourquoi il a été retenu.

## 5. Contrat proposé

V1 contractuelle, volontairement plus riche que `add/remove`, mais encore bornée :

```json
{
  "mandate_id": "2026-07-09:TW:preopen",
  "as_of": "2026-07-09T00:30:00Z",
  "valid_until": "2026-07-10T00:30:00Z",
  "venue": "TW",
  "baseline": {
    "default_hotlist": ["2330.TW", "2454.TW"],
    "source": "radar_hysteresis"
  },
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
        "source_refs": ["news_brief:2026-07-09:semis_tw"]
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
      "reason": "redundant_with_existing_hotlist|weak_signal|bad_liquidity"
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

## 7. Lien avec l'analyste-news

Cette spec ne remplace pas l'analyste-news. Elle le consomme.

L'analyste-news transforme :

```text
items bruts, earnings, headlines macro, événements
```

en :

```text
situation_state par symbole/famille/venue/marché
```

La passe univers transforme ensuite :

```text
radar + régime + situation_state + portefeuille + sticky
```

en :

```text
hotlist + mandat + contexte symbole
```

Si l'analyste-news n'est pas prêt, la V1 peut tourner avec :

- radar ;
- régime famille ;
- portefeuille ;
- news counters/logs existants ;
- champs `company_context.summary = "not_available"` explicites.

## 8. Lien avec allocation/risk

Le mandat univers peut d'abord être **informatif** :

- posture injectée au trader ;
- log dans `decisions.jsonl` ;
- dashboard d'audit.

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

## 10. Audit recommandé avant code

Faire un audit séparé, idéalement avec sous-agents par axe, avant d'implémenter :

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
   - Tests sticky : une position/plan ouvert ne disparaît pas du scope.

## 11. Phasage proposé

### Phase 0 — audit et issue map

Livrables :

- cartographie des responsabilités actuelles ;
- issue parent `Universe intelligence pass`;
- issues enfants : contrat, analyste-news dependency, context injection,
  policy observe/enforce, clean migration.

Aucun changement runtime.

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

### Phase 4 — analyste-news/fondamentaux

Remplacer les `company_context: not_available` par des briefs digérés :

- symbol situation ;
- family situation ;
- catalysts forward ;
- narrative backward ;
- source refs et TTL.

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

## 13. Critères d'acceptation V1

- La baseline déterministe continue de produire la même hotlist quand
  l'intelligence pass est désactivée.
- L'override LLM peut toujours être réduit à `add/remove` pour compat.
- Un mandat enrichi est produit, daté, TTLé, sourcé, et traçable par
  `mandate_id`.
- Le trader symbole peut recevoir une tranche de mandat sans voir toute la
  hotlist ni les sources brutes.
- Les sticky restent protégés.
- Les décisions peuvent être reliées au mandat actif dans le ledger.
- Un échec du LLM d'univers conserve le comportement déterministe actuel.
- Les tests distinguent bien :
  - sélection d'univers ;
  - contexte/mandat ;
  - décision symbole ;
  - admission/risk.

## 14. Questions ouvertes

1. Le mandat univers est-il produit une fois par venue en pré-open, ou une fois
   par jour cross-venue avec refresh par venue ?
2. Faut-il stocker les rejets détaillés (`rejects`) ou seulement les raisons
   agrégées pour ne pas créer de bruit ?
3. Quelle granularité pour `allowed_sides` : symbole seulement, famille, venue,
   ou portefeuille global ?
4. Quel TTL par défaut pour un mandat : une session, une journée, jusqu'à
   prochaine rotation, ou dépendant du signal ?
5. Quand l'analyste-news signale une situation entreprise contraire au régime
   famille, qui prime : exception symbole ou posture famille ?
6. Quelle surface cockpit minimale : hotlist enrichie, posture par famille,
   raisons de rejet, ou timeline du mandat ?

## 15. Issue map suggérée

- **Parent** : `Universe intelligence pass : hotlist enrichie, mandat et contexte symbole`.
- **Child A** : audit hexa de `trader/market/rotation` et plan de migration.
- **Child B** : contrat `UniverseMandate` + projection observe.
- **Child C** : injection du mandat dans `decide_one` sans enforcement.
- **Child D** : compilation `posture/allowed_sides` vers policy observe/enforce.
- **Child E** : raccord analyste-news/situation_state.
- **Child F** : dashboard/read model `universe_mandate`.
