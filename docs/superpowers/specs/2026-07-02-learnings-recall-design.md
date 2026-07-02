# Recall des learnings — mémoire outcome-weighted pour l'agent runtime

**Date** : 2026-07-02
**Status** : design approuvé sur le principe (session Erwan 02/07) ; plan d'implémentation à écrire
**Amont** : `docs/specs/2026-07-02-agent-data-lifecycle.md` (§ learnings),
`docs/specs/2026-07-02-learnings-memory-sota.md` (SOTA + décisions),
`docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md` (la couche d'outils hôte).

---

## 1. Contexte et problème

La mémoire actuelle de l'agent est un push linéaire de 15 slots consolidés
(10 global + 5/symbole) injectés à l'identique dans chaque prompt batch
(`daemon.py` → `build_context_learnings`). Mesures du 02/07 :

- 2 067 learnings émis depuis le 08/06 ; 89 % étaient perdus avant le
  quick-win d'archivage (8fdeb4c) + backfill (1 867 notes récupérées du ledger).
- Bootstrap FLAIR (`scripts/learnings_outcome_bootstrap.py`) : **34,1 % de
  win rate global** des notes scorables — mais dispersion extrême par
  symbole : GC=F 97 %, TTE.PA 100 %, NVDA 70 % vs NG=F 3 %, INGA.AS 6 %.
  ~1/3 seulement de la mémoire historique est validée par le forward.

Le push linéaire injecte donc du non-vérifié à égalité avec du vérifié, sans
pertinence contextuelle, avec un plafond de 15 slots écrasés en continu.

## 2. Goals

- Remplacer (progressivement) le push linéaire par un **pull contextuel** :
  l'agent demande sa mémoire pertinente via un outil borné.
- Pondérer le rappel par **l'outcome vérifié** (FLAIR, puis MemRL), la
  **fraîcheur/validité temporelle**, et la **pertinence** (facettes + texte).
- Conserver TOUT (aucune compression destructive) ; la consolidation devient
  une curation (fusion/généralisation), pas un écrasement.
- Mesurable au forward : A/B via `decision_bench` (jamais un benchmark IR
  générique).

## 3. Non-goals

- Pas de GraphRAG/extraction d'entités LLM (les entités sont déjà
  structurées ; cf. SOTA doc).
- Pas de fine-tuning d'embeddings avant ~7k notes.
- Pas de mode dégradé embeddings dédié (décision Erwan : l'API OpenAI sera
  joignable ; l'échec ponctuel relève du contrat d'outil standard —
  outcome=error compact, la décision continue sans recall).
- Pas de retrait du consolidé/guardrails du prompt en V1 (mesurer d'abord le
  recall en PARALLÈLE du push existant ; le retrait du push = décision
  ultérieure sur mesure).

## 4. Architecture

### 4.1 Store — SQLite unique : `state/learnings.db`

> Arbitrage SQLite vs DuckDB (question Erwan 02/07) : SQLite pour le store
> chaud (OLTP : record_recall fréquent pendant que le daemon lit → WAL ;
> FTS5 incrémental ; stdlib ; format stable à vie). DuckDB retenu comme
> OPTION pour l'analytique offline des phases ③/④ (il requête directement
> les archives `*.jsonl.gz` via read_json_auto — calibration τ, MemRL,
> bench) sans y persister, donc sans son problème de format entre versions.

Table `notes` :
```
id INTEGER PK, decision_id TEXT UNIQUE, ts TEXT, symbol TEXT, family TEXT,
venue TEXT, action TEXT, intent TEXT, executed INTEGER, reason TEXT,
note TEXT, concepts TEXT (JSON, vocabulaire TraderNexus),
source TEXT (runtime|ledger-backfill|consolidated),
valid_from TEXT, valid_until TEXT NULL, superseded_by INTEGER NULL,
verdict TEXT (WIN|LOSS|NEUTRAL|UNKNOWN), forward_return REAL NULL,
outcome_score REAL NULL,        -- FLAIR v1 (voir 4.2)
q_value REAL NULL,              -- MemRL (phase ③)
embedding BLOB NULL             -- OpenAI, pré-calculé batch
```
+ FTS5 sur `note` ; table `recalls` (phase ②) : {decision_id, note_ids JSON,
ts} — la trace de QUELLES notes ont servi QUELLE décision (comble le gap
d'attribution multi-note identifié dans la littérature).

Ingestion : `learnings-from-ledger.jsonl` (backfill) + `learnings-evicted.jsonl`
+ buffer vif + consolidés historisés, dédup par decision_id. Job incrémental
(nouvelles notes du ledger/buffer) exécutable offline et au démarrage daemon.

### 4.2 Scoring — FLAIR **normalisé par symbole** (exigence Erwan 02/07)

Le win rate absolu favoriserait les notes des symboles « faciles ». Score v1 :

```
base_rate(sym)   = win_rate des décisions scorables du symbole (fallback famille, puis global)
lift(note)       = win_indicator(note) − base_rate(sym)       # par note, V1 1 note = 1 décision
outcome_score    = shrunk_lift = (n·lift_moyen + k·0) / (n + k)   # shrinkage bayésien vers 0, k≈5
```

En V1, `n=1` par note (sa décision d'origine) → le shrinkage tire fort vers 0 :
c'est voulu (une seule observation ≠ vérité). Le score se raffine quand la
table `recalls` (phase ②) donne de vraies apparitions multiples par note.
Bande de scoring par classe d'actif à prévoir (les FX sortent tous NEUTRAL
avec la bande actions 0,5 % — finding bootstrap).

### 4.3 Retrieval — hybride, dans le contrat d'outil

`recall_learnings{symbol?, family?, concept?, query?, limit?}` (V1 : limit ≤ 8) :
1. filtre facetté SQL (symbol/family + validité temporelle) ;
2. candidats texte : FTS5 (BM25) + cosine (embedding OpenAI de la query,
   brute-force sur le sous-ensemble) ; fusion RRF k=60 ;
3. score final = RRF ⊕ `outcome_score` ⊕ decay `exp(−Δt/τ)` (τ=30 j en V1,
   calibration 7 j vs 30 j en phase ③) ;
4. résultat compact : {note (tronquée), ts, symbol, verdict, outcome_score} —
   même format borné que les autres outils.

Handler dans `TOOL_REGISTRY` (mêmes invariants : lecture seule, erreurs
compactes, budgets de tournée inchangés). Le provider (connexion sqlite +
embedder OpenAI) est injecté par le daemon via `ToolContext`.

### 4.4 Traçage des injections (phase ②, en même temps que l'outil)

Chaque recall servi écrit dans `recalls` les note_ids retournés + le
decision_id du cycle. Jointure ultérieure avec l'attribution → vraies
apparitions multiples → FLAIR affiné puis MemRL (`Q += α(r−Q)`, update
asynchrone post-outcome).

## 5. Invariants

- L'anti-auto-renforcement D6 est renforcé : le recall PRIORISE les notes
  validées (outcome_score>0) ; les notes UNKNOWN récentes restent accessibles
  mais marquées `verdict:UNKNOWN` — l'agent voit la différence.
- Aucune écriture par l'outil (la table `recalls` est écrite par le daemon,
  pas par le handler).
- Le store est reconstructible from scratch depuis les archives (le .db est
  un dérivé, pas une source de vérité — les JSONL restent canoniques).
- Latence recall < 1 s (dont ~100-300 ms d'embedding query OpenAI).

## 6. Rollout

②a store + ingestion + scoring (offline, testable seul) → ②b outil
`recall_learnings` + trace `recalls` (flag tools déjà actif) → mesurer 1-2
semaines (taux d'usage, notes servies, latence) → ③ decay calibré + MemRL →
④ bench A/B `decision_bench` → décision : retirer le push linéaire ou pas.

## 7. Tests (extraits)

- Ingestion idempotente (re-run backfill = 0 doublon).
- Scoring : lift par symbole correct sur fixtures (base rates distincts),
  shrinkage borne les n=1.
- Retrieval : filtre facettes strict, RRF stable, notes hors validité exclues,
  résultat borné en taille/nombre.
- Outil : contrat identique aux 8 existants (validation args, outcome enum,
  budget par symbole).
- Reconstruction : drop du .db + réingestion = mêmes scores.
