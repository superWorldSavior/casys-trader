# Abstraction data sources — routage multi-API, profils paper/prod

**Date** : 2026-06-10
**Statut** : ✅ LIVRÉ 2026-07-02 — Protocol DataSource + CompositeDataSource + config/data_sources.yaml + observabilité data_source implémentés (trader/tools/data_source.py, daemon.py:2010,2545) ; P2/P3 annulés §2bis
**Origine** : post-mortem `docs/postmortems/2026-06-09-short-clf-hard-stop.md` —
le daemon tourne en données IB différées (~15 min, `market_data_type=3`),
décision de ne PAS prendre d'abonnement IB pour le moment.

---

## 1. Problème

- Le runtime décide et coupe ses stops sur des prix vieux de ~15 min : les
  scalps (<1h de détention moyenne) sont aveugles, et les fills paper se font
  à des prix irréalisables → le forward paper perd sa valeur de preuve.
- L'abonnement IB temps réel est écarté pour l'instant (décision 2026-06-10).
- Contrainte long terme : **on tradera en réel sur IB** → les symboles et
  l'exécution doivent rester compatibles IB ; la data peut venir d'ailleurs.

⚠️ Ce design **amende la décision du 2026-06-08** « IB = SOURCE UNIQUE
runtime » : IB reste la voie d'exécution unique (et la data en profil
`prod`), mais la **data du profil `paper` devient multi-sources**.

## 2. Étude des sources (recherche 2026-06-10, sources citées en fin de doc)

| Classe | Source recommandée | Fraîcheur | Coût |
|---|---|---|---|
| ETF/actions US (SPY, QQQ, NVDA) | **Alpaca** free tier (IEX) | ~secondes | 0 $ |
| Forex (EURUSD, USDJPY) | **OANDA v20** (compte démo) | temps réel | 0 $ |
| Futures CME (CL=F, GC=F, NG=F) | yfinance (~10 min) ou IB (~15 min) | différé | 0 $ |
| TWSE (2330.TW…) | TWSE OpenAPI (officiel, sans clé) | ~15-20 min | 0 $ |
| ^FCHI | yfinance | ~15 min | 0 $ |

Incompressible sans payer : futures CME temps réel = Databento **179 $/mois**
(pay-as-you-go supprimé 04/2025) ; Euronext et TWSE temps réel également
payants (TWSE RT officiel ≈ 1 800 $/mois ; piste Fugle à vérifier).
Caveats : Alpaca IEX ≈ 2 % du volume consolidé (prix légèrement décalé, OK
paper) ; OANDA = prix indicatifs broker ; yfinance = API non-officielle,
fragile, fallback seulement.

## 2bis. Simplification retenue (Erwan, 2026-06-10)

« On peut pas tout prendre sur yfinance pour le moment ? et en prod on paiera
IB. » → **Profil `paper` = yfinance partout SAUF forex → IB** (IDEALPRO est
temps réel gratuit chez IB, alors que Yahoo FX est différé — c'est le constat
qui avait motivé le branchement IB le 08/06). **Profil `prod` = IB intégral**
avec souscriptions (~15-25 $/mois CME + US securities) et
`market_data_type=1`. Conséquences :
- Zéro compte/clé/adapter à créer : `market.get_bars` (yfinance) et
  `IBDataSource` existent déjà tous deux derrière l'interface `Bar`.
- Alpaca/OANDA (§2) deviennent des options futures si yfinance casse.
- Le risque yfinance (prix gelés hors séance, ToS, throttling) est assumé en
  paper : la garde `assess_freshness` reste le fusible et coupe en HOLD.
- P1 du phasage = Protocol + Composite + routes yfinance/IB. P2/P3 annulés.

## 3. Décisions de design (proposées)

1. **Symboles canoniques inchangés** : `config/universe.yaml` garde les
   identifiants actuels (style Yahoo). Chaque adapter fait son propre mapping
   (comme `config/ib_contracts.yaml` aujourd'hui) → zéro impact sur l'agent,
   le mandat, les learnings, les stats.
2. **Contrat unique existant** : le protocole implicite
   `get_bars(symbol, lookback, interval) -> list[Bar]` (déjà honoré par
   `IBDataSource` et `market.get_bars`) devient un `Protocol` explicite
   `DataSource`. Pas de God object : un adapter = une API.
3. **`CompositeDataSource`** : route chaque symbole vers une liste ordonnée
   de sources (primaire → fallbacks). Le fallback ne se déclenche que sur
   erreur ou data stale (la garde `market.assess_freshness` existante reste
   LE fusible — une source qui ment sur sa fraîcheur est coupée comme
   aujourd'hui).
4. **Config déclarative** `config/data_sources.yaml` (explicit over
   implicit, zéro default magique) :

   ```yaml
   profile: paper        # paper | prod — choisi ici, overridable par CLI/env
   profiles:
     paper:
       routes:
         - symbols: [EURUSD=X, USDJPY=X]
           sources: [ib, yfinance]   # IDEALPRO temps réel gratuit
         - symbols: ["*"]
           sources: [yfinance, ib]   # défaut : yfinance, fallback IB
     prod:
       routes:
         - symbols: ["*"]
           sources: [ib]             # market_data_type=1 + souscriptions
   ```

5. **Secrets** dans `.env` (`TRADER_ALPACA_KEY_ID/SECRET`,
   `TRADER_OANDA_TOKEN/ACCOUNT`), même pattern que le fallback Ollama.
6. **Exécution inchangée** : SimBroker en paper, IB en prod plus tard. Cette
   spec ne touche QUE la data.
7. **Observabilité** : chaque décision enregistre déjà `code_version` ; on
   ajoute `data_source` par symbole dans le snapshot marché (savoir avec
   quelles données chaque décision a été prise — même logique que le
   post-mortem CL=F).

## 4. Phasage proposé

- **P1** — `DataSource` Protocol + `CompositeDataSource` + config routing +
  adapter **Alpaca** (SPY/QQQ/NVDA en temps réel). Le reste route vers IB
  comme aujourd'hui. Gain immédiat : les actions US deviennent mesurables
  proprement.
- **P2** — adapter **OANDA** (FX temps réel ; remplace aussi la béquille
  « EURUSD frais le dimanche soir » côté veille).
- **P3** — adapter **TWSE OpenAPI** (différé mais débloque les 6 titres TW
  qui sont aujourd'hui quasi toujours `stale_market_data`).
- **P4 (optionnel)** — comparer empiriquement yfinance (~10 min) vs IB
  (~15 min) sur CL=F et router les futures vers le moins pire.

## 5. Conséquence stratégique — ACTÉE (Erwan, 2026-06-10)

Les futures CME restent différés quel que soit le montage gratuit →
**option (b) retenue : futures sortis de l'univers actif** pour la phase de
calibration (`config/universe.yaml`, CL=F/NG=F/GC=F commentés, contrats IB
conservés). Univers de calibration = actions US (data fraîche) + FX (temps
réel IB) + ^FCHI/TW (différés, sous garde stale). **Retour des futures en
prod** avec la souscription IB temps réel.

## 6. Sources

Alpaca docs (IEX free, 200 req/min) ; OANDA v20 (`pip install v20`, compte
démo) ; Databento blog 04/2025 (fin du pay-as-you-go CME, plans 179 $/mois) ;
CME delayed quotes (10 min) ; TWSE OpenAPI (openapi.twse.com.tw) ; Fugle
developer.fugle.tw (RT TW à vérifier) ; audits dev 2026 sur les délais réels
des free tiers Finnhub/Twelve Data (claims « real-time » non fiables).
