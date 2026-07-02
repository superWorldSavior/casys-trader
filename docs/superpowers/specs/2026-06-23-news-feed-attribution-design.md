# Design — Brique d'ingestion « fil d'actu » (phase attribution)

Date : 2026-06-23
Auteur : Erwan + Claude
Statut : LIVRÉ 2026-07-02 — phase attribution complète (news_feed.py + daemon câblage + decision_ledger + tests TDD) ; phase 2 veto/earnings hors scope


## Contexte & objectif

Le daemon décide EXECUTE/HOLD sans aucune conscience des catalyseurs
d'actualité (earnings, news). Plusieurs incidents passés (fakeouts d'open EU
armés, voir `[[casys-trader-finding-eu-open-armed-fakeout]]`) auraient pu être
filtrés par une conscience earnings.

Objectif de **cette** brique : ingérer un fil d'actu et **logger** deux signaux
au moment de chaque décision, **sans influencer la décision en live**. C'est une
phase de **mesure d'abord** : on prouve que le signal news a une valeur
prédictive (corrélation avec la qualité des trades) **avant** de le laisser
toucher la boucle de décision.

Principe directeur : *code over instructions* — faits calculés/injectés par le
code, prose minimale (`[[erwan-code-over-instructions]]`).

## Décision de source

Univers réel (`config/universe.yaml`) : Euronext (`.PA`, `.BR`), Taiwan
(`.TW`, `.TWO`), une pincée d'US. Critère discriminant = couverture native
multi-places.

| Source | Couverture univers réel | Fiabilité | Coût | Verdict |
|---|---|---|---|---|
| **Yahoo (`yfinance`)** | native `.PA/.BR/.TW/.TWO/US` | fragile (non-officiel) | 0 | **Primaire** |
| Finnhub free | US + ADR only (≈ MSFT seul ici) | robuste, structuré | 0 (clé posée) | **Dormant** |
| EODHD | native tout | robuste | ~10-20 $/mois | Non retenu (inutile vu l'univers) |

Décisions :
- **Yahoo primaire.** `yfinance` est **déjà** une dépendance (`trader/tools/market.py`
  l'utilise pour les bars) → même provider que les prix, zéro nouvelle dépendance.
  Les suffixes Yahoo = symboles natifs de l'univers (pas de mapping ADR).
- **Finnhub dormant.** Clé `TRADER_FINNHUB_API_KEY` enregistrée dans `.env`.
  Câblé seulement si la phase de mesure justifie un cross-check sur la portion US.
- Fragilité de `yfinance` acceptable : en phase attribution un échec source =
  perte de logging, **jamais** une décision de trading erronée.

## Architecture

### Module `trader/tools/news_feed.py`

À côté de `market.py`, même pattern (provider yfinance, cache).

Contrat unique, narrow :

```python
def news_snapshot(symbol: str, *, now: datetime) -> dict:
    """
    {
      "earnings_in_h": float | None,   # heures jusqu'à la prochaine date earnings connue
      "news_coverage": str,            # "ok" | "empty" | "unmapped" | "error"
      "news_count": int,               # nb de news dans la fenêtre récente
      "source": str,                   # "yahoo" | "none"
      "asof": str,                     # timestamp ISO du snapshot
    }
    """
```

- `earnings_in_h` : via `Ticker.get_earnings_dates()`. `None` si aucune date
  future connue. (La conscience-session existante — `next_regular_session_open`
  dans `market.py` — permettra plus tard d'exprimer le délai en sessions.)
- `news_coverage` — le garde-fou anti faux-négatif silencieux, distingue :
  - `ok` : source a répondu, ≥1 news
  - `empty` : source a répondu, 0 news (≠ aveugle)
  - `unmapped` : symbole inconnu de la source
  - `error` : source en panne / exception
- `news_count` : nombre de news dans une fenêtre récente (ex. 7 j).

### Robustesse (safe default)

- `news_snapshot` **ne lève JAMAIS** : tout échec yfinance est capté →
  `coverage: "error"`, `source: "none"`. Le cycle de décision continue intact.
- **Cache TTL en mémoire** par symbole (daemon long-running) pour ne pas marteler
  Yahoo (rate-limit) à chaque cycle : news ~30 min, earnings ~1×/jour.

### Intégration & logging

- Appel dans `daemon.py` au moment de `record_decision` (≈ ligne 2060),
  **PAS** dans le contexte LLM de `_batch_decide` (≈ lignes 1109-1136).
  → la donnée est **loggée, jamais vue par l'agent en live**.
- Ajout d'un sous-objet `news: {...}` **au top-level** de la row dans
  `build_decision_row` (`trader/decision_ledger.py`, ≈ lignes 71-145), pas enfoui
  dans `runtime` (narrow contract, attribution dédiée).

## Flux de données

```
run_cycle (daemon.py)
  └─ record_decision(entry)              # ≈ ligne 1641
       ├─ news_snapshot(symbol, now)     # NEW — Yahoo via cache TTL
       │     → {earnings_in_h, news_coverage, news_count, source, asof}
       └─ build_decision_row(...)        # decision_ledger.py
             → row["news"] = <snapshot>  # NEW — top-level
             → append() vers state/decisions.jsonl
```

Le LLM (`codex_client.decide_batch`) ne reçoit RIEN de neuf en phase 1.

## Gestion d'erreurs

| Cas | Comportement |
|---|---|
| yfinance lève / timeout | `coverage: error`, `source: none`, cycle continue |
| symbole inconnu de Yahoo | `coverage: unmapped` |
| 0 news mais source OK | `coverage: empty` (≠ aveugle) |
| pas de date earnings future | `earnings_in_h: None` |
| cache chaud | retour immédiat, pas d'appel réseau |

## Tests (test-first invariants)

yfinance mocké. Invariants prioritaires sur le happy path :
- `news_snapshot` ne propage **aucune** exception (source en panne → `error`).
- `news_coverage` ∈ {`ok`,`empty`,`unmapped`,`error`} toujours.
- `earnings_in_h is None` quand aucune date future.
- `unmapped` sur symbole bidon.
- `empty` distinct de `error` distinct de `unmapped`.
- Cache : 2e appel dans le TTL ne déclenche pas d'appel réseau.

## Phasage

- **Phase 1 (cette brique)** : logging attribution, zéro influence live.
  Mesure 2-3 semaines de corrélation news ↔ qualité trade (via les nouveaux
  champs dans `decisions.jsonl`, croisés avec `decision_reason_code` / P&L).
- **Phase 2 (ultérieure, conditionnelle)** : si la mesure paye, promotion
  d'`earnings_in_h` en veto / dé-risque dans la décision (filtre de risque).
  Éventuel câblage Finnhub cross-check US à ce moment. **Hors scope de ce spec.**

## Hors scope

- Influence de la news sur la décision en live (Phase 2).
- Sentiment / NLP des news (mesurer la valeur du signal brut d'abord).
- Déclencheur de réveil news-driven (risque de whipsaw, écarté).
- EODHD / sources payantes.
- Câblage Finnhub actif.
