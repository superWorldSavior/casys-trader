# Axe macro/fondamental — esquisse d'architecture

**Date** : 2026-07-02
**Status** : cadrage validé sur le principe (Erwan) ; spec détaillée à écrire au lancement du chantier
**Amont** : `docs/specs/2026-07-02-macro-data-sources.md` (cartographie des sources),
pattern news-feed (`docs/superpowers/specs/2026-06-23-news-feed-attribution-design.md`),
couche d'outils (`2026-06-29-agent-domain-tools-design.md`).

## 1. Intention

Couvrir le pan manquant : macro (taux, inflation, calendrier éco) et
fondamentaux (rapports de sociétés) — « un agent capable de lire en parallèle
et de comprendre » (Erwan 02/07). Sources : cf. cartographie (US/EU gratuits,
FinMind pour TW, trou earnings calendar EU/TW ~60 €/mois si besoin).

## 2. Architecture en 3 étages

```
[Collecteurs]            [Analyste offline]           [Exposition à l'agent]
FRED / BCE / Eurostat →  job batch (cron/daemon      →  PUSH cockpit : FAITS
Finnhub earnings US      hors cycle) : un LLM           calendaires compacts
FinMind TW               (pas le brain) LIT et          calculés par le code
edgartools (10-K/Q US)   DISTILLE en briefs          →  PULL outils : briefs
ESEF EU (phase 2+)       STRUCTURÉS, datés, bornés      et fondamentaux à la
                         et VERSIONNÉS                  demande
```

- **Collecteurs** : un module par source, sortie JSONL datée append-only
  (même hygiène que les archives learnings). Aucun LLM.
- **Analyste offline** : batch (nocturne pour le macro, post-filing pour les
  rapports). Il produit des briefs courts structurés :
  `{zone|symbol, as_of, valid_until, points: [...], sources: [...]}` —
  jamais de prose libre non bornée. Le brain runtime ne lit JAMAIS les
  documents bruts (un 10-K = 100+ pages ; c'est le job de l'analyste).
- **Exposition** — réponse à la question cockpit :

### 2.1 PUSH cockpit : uniquement des faits calendaires calculés

Principe « code over instructions » (Erwan) : le cockpit reçoit des FAITS
que le code calcule, compacts et non ambigus. Ex. :

```json
"macro_next": [
  {"event": "FOMC", "in_h": 142},
  {"event": "CPI_US", "in_h": 38},
  {"event": "earnings", "symbol": "2330.TW", "in_h": 40}
]
```

Quelques dizaines d'octets. PAS d'analyse, PAS de brief dans le push.
(`earnings_in_h` par symbole existe déjà dans le payload news.)

### 2.2 PULL outils : la profondeur à la demande

- `get_macro_brief{zone?}` → le dernier brief macro de l'analyste (US/EU/TW).
- `get_fundamentals{symbol}` → les points clés du dernier rapport distillé
  (croissance, marges, dette, risques signalés au MD&A) + verdict de
  fraîcheur (date du filing).

Même contrat que les 9 outils existants : lecture seule, bornés, tracés dans
`runtime.tool_calls` → **l'usage et l'utilité seront mesurables par
l'attribution**, comme le recall.

### 2.3 Attribution-first, comme toujours

Phase 1 : les briefs sont produits et loggés par décision (champ dédié du
ledger, comme `news`) SANS exposition à l'agent. Phase 2 : exposition en
pull (outils). Phase 3 : promotion au push cockpit UNIQUEMENT de ce que la
mesure justifie (le veto earnings suivra ce chemin si la re-mesure de
fin juillet le justifie).

## 3. Phasage proposé

- **P1 — calendriers (faits)** : FRED (3 séries) + dates FOMC/CPI (scraping
  trimestriel des pages Fed/BLS) + Finnhub earnings US + investigation de la
  couverture earnings actuelle (en cours 02/07 : buckets suspects). Livrable :
  `macro_next` dans le payload d'attribution (pas encore au cockpit).
- **P2 — analyste fondamental US+TW** : edgartools (MD&A/risk factors des
  tickers US du portefeuille, post-filing) + FinMind (états financiers TW) →
  briefs par symbole loggés.
- **P3 — outils de pull + mesure** : `get_macro_brief`/`get_fundamentals` au
  registre, mesure d'usage/outcome, puis décisions de promotion au cockpit.
- **P4 (optionnel, sur mesure)** : EODHD ~60 €/mois si le calendrier EU/TW
  prospectif prouve son manque.

## 4. Invariants

- Le brain runtime ne lit jamais de document brut ni n'appelle une API
  externe de données : il consomme des briefs bornés produits offline.
- Tout ce qui entre au cockpit est un fait code-calculé.
- Chaque étage est mesurable avant d'être promu (attribution-first).
- Les collecteurs sont indépendants et remplaçables (AX composable).
