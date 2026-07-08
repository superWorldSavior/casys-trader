# Référence — Données macro (calendrier + séries)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/market/macro_calendar`, `trader/infrastructure/market_sources/macro_series` · **Phase** : P1a (collecte, en livraison 2026-07-02)
> **Spec** : chantier macro/fondamental (`docs/superpowers/specs/2026-07-02-macro-*`)

Deux briques de **collecte** macro, distinctes. Phase P1a = persister la donnée ;
l'analyste-news unique (P2) et l'usage LLM viennent après (stock de 2-3 semaines
requis).

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

## État actuel (🟡)

P1a = **collecte + persistance** uniquement. La donnée macro **n'est pas encore
vue par le LLM en décision** (comme le fil news : phase attribution d'abord,
promotion après mesure). Voir la carte de couverture et le registre pour la suite.

## Voir aussi
- [Config](config.md) (`data_sources.yaml`, `sessions.yaml`) · fil news (`infrastructure/market_sources/news_feed`).
