# Sources de données macro/fondamentales — cartographie 2026 (US+EU+TW)

**Date** : 2026-07-02
**Statut** : cadrage du chantier macro/fondamental — 4 recherches web + synthèse
**Contexte** : agent-analyste offline (pattern news-feed attribution-first), univers US+EU+Taïwan, budget gratuit → quelques dizaines d euros/mois.

## 1. Carte des meilleures options

| Pan | Zone | Meilleure option gratuite | Meilleure option payante | Coût |
|---|---|---|---|---|
| **Macro** | US | FRED (`fredapi`) + BLS scraping HTML + Fed scraping HTML | idem, rien à payer | 0 |
| **Macro** | EU | ECB Data Portal (`ecbdata`) + Eurostat (`eurostat`) | idem | 0 |
| **Macro** | TW | OECD API "Chinese Taipei" (partiel) + CBC PDF trimestriel manuel | EODHD macro TW | ~€60/mois |
| **Rapports / Fondamentaux** | US | SEC EDGAR + `edgartools` (XBRL + MD&A markdown) | idem, rien à payer | 0 |
| **Rapports / Fondamentaux** | EU | `xbrl-filings-api` sur filings.xbrl.org (ESEF, post-2020 seulement) | EODHD Fundamentals | €60/mois |
| **Rapports / Fondamentaux** | TW | FinMind (300-600 req/h, états financiers trimestriels MOPS/TWSE) | idem | 0 |
| **Earnings calendar** | US | Finnhub free `/calendar/earnings` (déjà dans le stack) + FMP free 250 req/j | idem | 0 |
| **Earnings calendar** | EU | **Aucune viable gratuite avec API** | EODHD All-In-One ou FMP Ultimate | €60-149/mois |
| **Earnings calendar** | TW | **Aucune prospective gratuite** | EODHD (TWSE/TPEx confirmé) | €60/mois |

---

## 2. Verdict global

**Ce qui est vraiment facile en 2026 (bloquant historique levé) :**

- **Macro US** : FRED + BLS HTML + Fed HTML = couverture quasi-complète, gratuit, APIs stables. Zéro friction.
- **Macro EU** : ECB Data Portal + Eurostat = taux/inflation/PIB EU officiels et gratuits, libs Python en 2 lignes.
- **Filings US** : EDGAR/edgartools = exemplaire. XBRL structuré depuis 2009, MD&A/Risk Factors en markdown propre, MCP server embarqué. Rien de comparable EU ou TW, mais pour les US c'est résolu.
- **Fondamentaux TW** : FinMind est une vraie surprise — gratuit, bien maintenu, états financiers trimestriels MOPS/TWSE structurés. Complètement méconnu.
- **Filings EU post-2020** : filings.xbrl.org couvre le CAC 40, DAX, AEX, MIB via ESEF. La lib `xbrl-filings-api` évite le scraping AMF/Bundesanzeiger. Parsing XBRL à coder, mais faisable.

**Ce qui reste vraiment dur :**

- **Earnings calendar EU/TW prospectif** : c'est le vrai trou. Pas de source gratuite avec API propre couvrant les dates futures d'annonces pour des tickers Paris, Xetra, Milan ou TWSE. Investing.com = ToS interdits, point final.
- **Fondamentaux EU normalisés pré-2020** : ESEF n'est obligatoire que depuis 2020. Avant = uniquement agrégateurs payants (EODHD, Refinitiv). Pas de contournement gratuit.
- **Macro TW actionnable** : CBC sans API machine-readable officielle. OECD couvre "Chinese Taipei" en données harmonisées mais avec retard, pas en temps quasi-réel.
- **Consensus analystes** : universellement payant. Aucun contournement gratuit fiable.
- **Transcripts EU/TW** : aucune agrégation gratuite. Scraping pages IR société par société = faisable sur 10-15 titres, pas scalable au-delà.

**Le blocage historique est partiellement dépassé.** Pour US (macro + filings + fondamentaux) et TW (fondamentaux), la situation a changé significativement. Pour EU et le calendrier earnings multi-marchés, le blocage reste réel — mais le coût de déverrouillage est de ~€60/mois, pas Bloomberg.

**Incertitudes à signaler :**
- yfinance ToS interdit l'usage commercial automatisé — usage R&D paper trading est dans une zone grise tolérée, pas garantie.
- FinMind : open source single-maintainer-ish, rate limits non contractuels.
- filings.xbrl.org : infrastructure OAM-dépendante, sans SLA.
- Finnhub `/calendar/earnings` EU/TW : confirmé US-only en free tier, à ne pas supposer international sans tester.

---

## 3. Reco de démarrage (max valeur / min plomberie)

**Phase 1 — Gratuit, s'appuie sur l'existant, 2-3 jours de travail :**

1. **FRED** (`pip install fredapi`) → injecter taux Fed Funds + 10Y yield + CPI US dans le contexte LLM. 3 séries, 1 appel quotidien. C'est le signal macro US le plus actionnable.

2. **ECB Data Portal** (`pip install ecbdata`) → taux dépôt BCE + €STR. 2 séries, pertinent pour les sessions EU. Même pattern que FRED.

3. **Finnhub `/calendar/earnings` US** (clé déjà en `.env`) → tester immédiatement sur 7 jours glissants pour le sous-ensemble US du portfolio. Si ça passe = earnings calendar US sans surcoût.

4. **FinMind** pour les tickers TW → états financiers trimestriels. Même pattern que news-feed attribution-first : distiller offline, logguer par symbole, ne pas injecter à chaque tick. TW = volet le plus surprenant en terme de gratuité.

5. **edgartools** (`pip install edgartools`) → pour les tickers US du portfolio : `tenk.mda` + `tenk.risk_factors` en markdown, 1 fois par trimestre post-filing. Réutiliser exactement le pattern news-attribution-first : l'agent distille offline, le résumé est injecté comme contexte lors des sessions d'analyse.

**Ce qu'on n'attaque PAS en phase 1 :** earnings calendar EU (pas de solution gratuite), filings EU (parsing XBRL à coder, plus long), transcripts (scraping IR manuel = faible ROI initial).

**Principe de séquençage :** US d'abord (EDGAR + FRED + Finnhub earnings), TW en parallèle (FinMind), EU en phase 2 quand le pipeline est rodé.

---

## 4. Budget total réaliste

| Scénario | Sources payantes | Ce que ça déverrouille | Coût mensuel |
|---|---|---|---|
| **Option 0 — tout gratuit** | aucune | Macro US/EU complet, filings US, fondamentaux TW, filings EU 2020+, earnings US | 0 |
| **Option 1 — un achat ciblé** | EODHD Fundamentals Data | + Fondamentaux EU normalisés (60+ exchanges) + Earnings calendar EU + TWSE/TPEx earnings calendar | **€60/mois** |
| **Option 2 — one-stop-shop** | EODHD All-In-One | Tout ci-dessus + prix globaux + macro étendue | **€100/mois** (annuel ~€60) |
| **Option 3 — transcripts inclus** | FMP Ultimate | Earnings + fondamentaux + transcripts earnings calls, mais EU/TW moins vérifié que EODHD | **$149/mois** |

**Recommandation budgétaire :** commencer par l'Option 0 (phase 1 ci-dessus), mesurer la valeur ajoutée sur 3-4 semaines, puis décider si EODHD €60/mois est justifié. L'earnings calendar EU est le seul vrai manque bloquant — et ce n'est bloquant que si l'agent-analyste veut anticiper les dates d'annonce plutôt que réagir après. Pour un pattern attribution-first (analyser les fondamentaux, pas les surprises d'earnings), l'Option 0 couvre 80% du besoin.