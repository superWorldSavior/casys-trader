# Analyste-news quotidien + calendriers macro (P1-P2 de l'axe macro)

**Date** : 2026-07-02
**Status** : COLLECTE (P1a) en implémentation immédiate (décision Erwan 02/07 :
« brancher les sources et collecter d'abord ») ; l'analyste (P2) attendra
~2-3 semaines de stock d'items.
**Amont** : `2026-07-02-macro-fundamental-design-sketch.md` (architecture 3 étages,
phasage réordonné), `docs/specs/2026-07-02-macro-data-sources.md` (sources),
pattern consolidateur (`trader/consolidator.py`) et news-feed
(`trader/tools/news_feed.py`).

## 1. Périmètre

P1 (calendriers-faits) + P2 (analyste-news quotidien) uniquement. Rapports/
fondamentaux (P4) et `ask_analyst` (P5) hors périmètre. Principe directeur
(Erwan) : UN agent lit le flux une fois et distribue une synthèse bornée —
le flux brut n'entre jamais dans un contexte de décision.

## 2. P1 — calendriers macro (faits code, zéro LLM)

### 2.1 Collecteur `trader/macro_calendar.py`

- **FOMC** : dates des réunions 2026 publiées un an à l'avance —
  V1 : constante versionnée dans le module (8 dates connues) + TODO scraping
  trimestriel de federalreserve.gov/monetarypolicy/fomccalendars.htm
  (structure HTML stable). Le scraping N'EST PAS dans le chemin du daemon :
  script offline qui met à jour un JSON (`state/macro_calendar.json`).
- **CPI/NFP US** : dates BLS (bls.gov/schedule/2026/home.htm) — même
  mécanique : script offline → JSON, le daemon ne fait que LIRE le JSON.
- **BCE** : dates du Conseil des gouverneurs (pages HTML BCE) — idem.
- Format du JSON : `[{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}, ...]`,
  append/écrasé par le script, versionnable dans le temps.

### 2.2 Exposition P1 : payload d'attribution d'abord

`macro_next` calculé par le code au moment de la décision (comme
`earnings_in_h`) : les 3 prochains événements avec `in_h`. Logged dans le
payload news/macro de chaque décision. PAS encore au cockpit (promotion
sur mesure, comme le veto earnings).

## 3. P2 — l'analyste-news quotidien

### 3.1 Prérequis : persister les items de news (manque actuel)

Le payload actuel ne stocke QUE des compteurs (`news_count`,
`news_coverage`) — les titres/résumés ne sont persistés nulle part.
Extension du collecteur (`news_feed.py`) : à chaque fetch réussi, appendre
les items nouveaux dans `state/news_items/YYYY-MM-DD.jsonl` :
`{fetched_at, symbol, title, publisher, published_at, link, uuid}` —
dédup par uuid par jour. Hygiène habituelle : append-only, un fichier/jour,
purge >60 j (même mécanique que radar_cache).

### 3.2 Le job analyste : `trader/news_analyst.py`

- **Déclenchement** : pattern consolidateur — évalué en fin de cycle daemon,
  exécute si (a) dernier brief > 20 h OU (b) jamais de brief aujourd'hui et
  heure locale > 07:00. Best-effort : un échec LLM ne touche pas au cycle
  (backoff comme `ConsolidationStatusStore`).
- **Entrée** : les items du jour (+ veille si premier run) depuis
  `state/news_items/`, groupés par famille (via `family_for_symbol`) +
  le `macro_next` courant.
- **LLM** : le routeur existant (`llm.build_default_router_from_env`, profil
  consolidateur — spark avec fallback), prompt de distillation avec contrat
  JSON strict (pattern `_parse_consolidated_json_from_text` réutilisable).
- **Sortie** : `state/news_briefs/YYYY-MM-DD.json` :

```json
{
  "as_of": "2026-07-03T07:10:00Z",
  "valid_until": "2026-07-04T07:00:00Z",
  "zones": {"US": [{"point": "...", "sources": ["uuid1"]}],
             "EU": [...], "TW": [...]},
  "families": {"semis": [{"point": "...", "symbols": ["2330.TW"], "sources": [...]}]},
  "alerts": [{"point": "...", "severity": "info|watch", "symbols": [...]}]
}
```

  Bornes : ≤5 points par zone, ≤3 par famille, chaque point ≤200 chars,
  chaque point CITE ses uuids sources (auditables vers news_items). Toute
  version remplacée est historisée (append `news_briefs-history.jsonl` —
  leçon du chantier learnings : rien ne s'écrase sans trace).

### 3.3 Exposition P2 : attribution-first

Le daemon logge dans chaque décision `brief_ref = {"date", "as_of"}` (le
brief actif au moment de la décision) — PAS le contenu. La jointure
décision×brief×outcome devient possible dès le premier jour. AUCUNE
exposition au LLM runtime en P2.

### 3.4 P3 (rappel, hors périmètre) : `get_macro_brief{zone?|family?}` au
registre d'outils, servant le brief du jour depuis le store — après mesure.

## 4. Invariants

- Le daemon ne fait AUCUN appel réseau macro dans le cycle : il lit des
  JSON locaux produits offline (calendriers) ou déclenche l'analyste en
  best-effort hors du chemin de décision.
- L'analyste ne voit que les items collectés (pas de navigation libre).
- Briefs bornés, datés, sourcés (uuids), historisés.
- Chaque étage mesurable avant promotion (attribution-first strict).

## 5. Tests clés (pour le plan)

- Collecteur items : dédup uuid, un fichier/jour, purge.
- macro_next : calcul in_h correct, événements passés exclus, JSON manquant
  → payload vide sans erreur.
- Analyste : déclenchement (20 h/07:00), échec LLM → backoff sans impact
  cycle, parsing strict avec réparation, bornes du brief appliquées,
  historisation de la version remplacée.
- brief_ref loggé par décision ; absent si aucun brief du jour.

## 6. Décisions prises / à confirmer en début d'implémentation

- Prises : refetch quotidien REJETÉ au profit de la persistance des items
  (historique intraday conservé, auditable) ; l'analyste utilise le routeur
  consolidateur existant ; POML non utilisé en P2 (prompt f-string pattern
  consolidateur) — réévaluer en P4 pour les 10-K.
- À confirmer : le seuil de purge news_items (60 j proposé) ; l'heure du
  brief (07:00 locale = avant l'ouverture EU, après la clôture TW).


## 7. Réordonnancement final (Erwan 02/07 soir)

**P1a — COLLECTE, immédiat** : (1) persistance des items news (le manque
identifié §3.1) ; (2) calendrier macro : dates FOMC/CPI/BCE connues →
state/macro_calendar.json + `macro_next` loggé par décision ; (3) séries
macro quotidiennes via DBnomics (zéro clé API, un connecteur pour
FRED/BCE/Eurostat, pattern _post_json sans dépendance) → append-only,
déclenchement best-effort quotidien par le daemon (pattern consolidateur).
**P2 — analyste : différé au stock** (~2-3 semaines d'items).
Le scraping des calendriers (Fed/BLS/BCE) reste offline et optionnel en V1 —
les dates 2026 sont connues et versionnées.
