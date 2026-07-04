# Référence — Fil d'actu (news)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/market/news_feed` · **Phase** : attribution (loggée, pas au LLM)
> **Design** : livré (historique dans `git log` ; cette page de référence fait foi)

Ingestion du fil d'actualité par symbole (Yahoo via yfinance). **Phase
attribution** : la news est **loggée par décision**, elle **n'est PAS vue par le
LLM** en live (mesure 2-3 semaines avant une éventuelle promotion en veto earnings).

## Ce qui est collecté

- **News récentes** par symbole (fenêtre `_NEWS_WINDOW_DAYS`, ~7 j).
- **Prochain earnings** : `_next_future_earnings` / `_earnings_in_h` → délai en
  heures avant le prochain résultat (`earnings_in_h`).
- Couverture : `ok` / `empty` / `unmapped` / `error`.

## Structure

- `RawNews` — **agrégat par symbole** : `mapped`, `earnings_dates`, `news_count`,
  `items` (les items bruts yfinance). Les champs titre/date/uuid vivent dans
  `_item_to_row()`, pas ici.
- `_news_published_at(item)` — date de publication robuste (`content.pubDate` &
  variantes yfinance).
- `_item_uuid(item)` — extrait l'uuid d'un item (le **dédup** est fait dans
  `NewsItemsArchive.append_items`).
- `reset_cache()` — vide le cache (TTL `CACHE_TTL_MINUTES`, ~30 min).

## Ce qui apparaît dans la décision

Bloc `news` estampillé par décision : `earnings_in_h`, `news_coverage`,
`news_count`, `source`, `asof`. **Non injecté au prompt** — sert à l'attribution
a posteriori (corréler décisions et actualité).

## Pièges yfinance (corrigés)

`content.pubDate` (pas `pubDate`), parseur `lxml`, `fast_info["last_price"]` sous
try/except (l'API yfinance lève au lieu de retourner None). Finnhub dormant
(free = US-only, inutile pour l'univers EU/TW).

## Voir aussi
- [Macro](macro.md) (même logique : collecte d'abord, LLM après) · [reporting](reporting.md).
