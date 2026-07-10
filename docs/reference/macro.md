# Référence — Données macro (calendrier + séries)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/market/macro_calendar`,
> `trader/infrastructure/market_sources/macro_series`,
> `trader/runtime/news_macro_runtime` · **Phase** : collecte + analyste async
> **Spec** : chantier macro/fondamental (`docs/superpowers/specs/2026-07-02-macro-*`)

Deux briques de collecte macro distinctes alimentent désormais l'analyste
macro/news. Elles ne sélectionnent aucun symbole et ne composent aucune hotlist.

## Calendrier — `market/macro_calendar`

Événements macro datés (FOMC, CPP/CPI, NFP, BCE). Le daemon lit
`state/macro_calendar.json` (produit par un **script offline**) et le **fusionne
avec des constantes versionnées** (les dates FOMC de l'année sont connues ~1 an à
l'avance).

- **FOMC** : statement publié à **18:00Z** le 2ᵉ jour de la réunion (réunion à 2
  jours). Source : federalreserve.gov. (Attention fuseau : jan/déc en EST → 19:00Z,
  cf. fix daemon.)
- Scraper **trimestriel** → `state/macro_calendar.json` pour CPI/NFP/BCE/FOMC N+1.
- Exposé au contexte via `macro_next` (prochain événement macro).

## Séries — `infrastructure/market_sources/macro_series`

Collecte **quotidienne** de séries macro via **DBnomics** (thread de fond).

- Marqueur `state/macro_series/.last_collect` — cooldown **20 h** (`COLLECT_COOLDOWN_H`), soit ~1×/jour.
- **Fail-safe total** : toute exception est avalée — n'affecte jamais le trading
  (écrit uniquement dans `state/macro_series/*`, aucun état partagé).
- Séries **versionnées** (v1, identifiants vérifiés DBnomics 2026-07-02), ex.
  `BLS/cu/CUSR0000SA0` = CPI US tous postes (mensuel).
- Note BCE : DBnomics/ECB peut avoir quelques jours de décalage vs la BCE.

## Analyste macro/news

Après la création du scope final à T-90 du pré-open, un runner asynchrone
single-flight construit un brief par venue à partir du calendrier, des derniers
points de séries, des headlines globales disponibles et des news du pool candidat,
y compris l'overnight. Le parent quantitatif de clôture est ignoré par ce runner.
Les briefs sont appendus dans `state/news_briefs/*.jsonl` puis indexés dans
`situation_memory.db` comme dérivé.

Ce brief est un constat sourcé. L'analyste ne choisit pas les candidats et ne
compose pas la hotlist. L'agent univers reste seul propriétaire de la sélection
des 25 non-sticky. Chaque brief porte le `candidate_scope_id` exact et sa
projection bornée est directement injectée au runner d'univers ; un brief absent
ou appartenant à un autre scope n'est jamais recyclé silencieusement.

La collecte et l'analyse sont best-effort : une panne macro ou analyste ne bloque
jamais le cycle de trading ni la rotation.

La couverture macro/news n'est pas présentée comme complète :

- le calendrier fusionne le fichier local avec des constantes versionnées de
  fallback ;
- certaines séries peuvent être absentes ou stales ;
- les headlines globales ne sont disponibles que si un producteur local a écrit
  `macro_headlines` ou `global_news_items` ; aucune source globale indépendante
  n'est garantie aujourd'hui ;
- la couverture news symbole dépend du corpus local effectivement collecté.

Le brief expose ces limites dans `input_refs.coverage`. Le contexte régime
consommé par l'agent univers vient du snapshot atomique
`state/last_regime.json`. Il porte son timestamp et une couverture actuellement
limitée à l'univers tradable actif ; après 96 h ses valeurs sont retirées du
prompt. Une famille absente est traitée comme couverture manquante, jamais comme
un régime neutre.

`CASYS_NEWS_MACRO_ANALYST_ENABLED=0` coupe ce runner. Il est activé par défaut,
mais un état live ancien ne produit pas de brief rétroactif : le daemon doit être
actif et une clôture doit d'abord matérialiser le scope de la venue. La préparation
univers possède son switch séparé,
`CASYS_UNIVERSE_INTELLIGENCE_ENABLED=0`.

## Voir aussi

- [Config](config.md) (`data_sources.yaml`, `sessions.yaml`)
- [News, challengers et analyste](news.md)
- [Gestion d'univers](universe-rotation.md)
