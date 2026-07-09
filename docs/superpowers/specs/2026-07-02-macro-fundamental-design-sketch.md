# Axe macro/fondamental — esquisse d'architecture

**Date** : 2026-07-02
**Status** : partiel — P1 livré 2026-07-02 (macro_next + FOMC/DBnomics, wired daemon + tests) ; P2 analyste-news batch à coder ; P3–P5 en attente
**Amont** : `docs/superpowers/specs/2026-07-02-macro-data-sources.md` (cartographie des sources),
pattern news-feed consolidé dans `docs/reference/news.md`,
couche d'outils consolidée dans `docs/reference/agent-tools.md`.

> **Statut 2026-07-09 : partiellement superseded.** Les collecteurs, l'analyste
> offline, les briefs bornés/sourcés et l'attribution-first restent valides. La
> partie **Exposition à l'agent** est raffinée par
> `docs/superpowers/specs/2026-07-09-universe-intelligence-pass-design.md` :
> les briefs analyste deviennent un `situation_state` consommé par la passe
> univers/PM, qui produit ensuite le mandat et le contexte symbole pour l'agent
> de trading.

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

Même contrat que les outils read-only courants (`docs/reference/agent-tools.md`) :
lecture seule, bornés, tracés dans `runtime.tool_calls` → **l'usage et
l'utilité seront mesurables par l'attribution**, comme le recall.

### 2.3 Attribution-first, comme toujours

Phase 1 : les briefs sont produits et loggés par décision (champ dédié du
ledger, comme `news`) SANS exposition à l'agent. Phase 2 : exposition en
pull (outils). Phase 3 : promotion au push cockpit UNIQUEMENT de ce que la
mesure justifie (le veto earnings suivra ce chemin si la re-mesure de
fin juillet le justifie).

## 3. Phasage (réordonné avec Erwan 02/07)

Principe directeur (Erwan) : « un agent unique qui lit, plutôt que toutes les
news dans tous les agents » — le flux brut n'entre JAMAIS dans un contexte de
décision ; il est lu UNE fois par l'analyste et distribué en synthèse bornée
(même logique que le consolidateur de learnings).

- **P1 — calendriers (faits)** : FRED (3 séries) + dates FOMC/CPI (scraping
  trimestriel Fed/BLS) + earnings (couverture Yahoo validée 02/07, saine).
  Livrable : `macro_next` dans le payload d'attribution.
- **P2 — l'analyste-news quotidien** (priorité Erwan) : job batch qui LIT le
  flux news déjà collecté (Yahoo par symbole + headlines macro) et produit LE
  brief du jour par zone/famille — structuré, borné, daté. Loggé
  attribution-first (pas encore exposé). S'appuie à 100 % sur la collecte
  existante.
- **P3 — exposition en pull + mesure** : `get_macro_brief` au registre,
  mesure d'usage/outcome par l'attribution, décisions de promotion (dont
  l'éventuel fait compact au cockpit).
- **P4 — rapports/fondamentaux** : edgartools (MD&A/risques US, post-filing)
  + FinMind (états financiers TW) par le même analyste → `get_fundamentals`.
- **P5 — `ask_analyst` interactif** (session ACP persistante `macro-analyst`)
  pour les questions ouvertes — seulement si la mesure de P3 montre que le
  pull facetté ne suffit pas. Timeout/budget stricts.
- **(sur mesure)** : EODHD ~60 €/mois si le calendrier EU/TW prospectif
  manque vraiment.

## 4. Invariants

- Le brain runtime ne lit jamais de document brut ni n'appelle une API
  externe de données : il consomme des briefs bornés produits offline.
- Tout ce qui entre au cockpit est un fait code-calculé.
- Chaque étage est mesurable avant d'être promu (attribution-first).
- Les collecteurs sont indépendants et remplaçables (AX composable).
