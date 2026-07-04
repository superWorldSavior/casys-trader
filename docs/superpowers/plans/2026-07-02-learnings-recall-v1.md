# Learnings Recall V1 — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implémenter le design `docs/superpowers/specs/2026-07-02-learnings-recall-design.md` : store SQLite outcome-weighted (scoring par symbole : lift + shrinkage), retrieval hybride, outil `recall_learnings` dans le registre, trace des injections.

**Architecture actuelle:** `trader/agent/learnings/embeddings.py` (client OpenAI minimal sur le pattern `_post_json` de `trader/agent/llm.py`) ; `trader/agent/learnings/store.py` (SQLite : schéma, ingestion idempotente, scoring, recherche hybride, table recalls ; ancien `trader.learnings_store` en compat) ; handler dans `trader/agent/tools/learnings.py` ; câblage provider + trace dans `trader/runtime/daemon.py`.

**Tech Stack:** Python 3.11, sqlite3 stdlib (FTS5 vérifié dispo), numpy (déjà présent), urllib via `llm._post_json` (aucune dépendance nouvelle). Embeddings `text-embedding-3-small` (clé `OPENAI_API_KEY` du `.env`).

## Global Constraints

- Le daemon LIVE tourne : ne jamais toucher aux fichiers de `state/` dans les tests (tmp_path uniquement). Le `.db` de prod sera créé par le script d'ingestion, PAS par les tests.
- Le `.db` est un DÉRIVÉ reconstructible — les JSONL d'archives restent canoniques.
- L'outil est lecture seule ; la table `recalls` est écrite par le daemon, jamais par le handler (invariant design §5).
- Résultats bornés : limit ≤ 8, notes tronquées à 240 chars dans la sortie outil.
- Aucun secret en dur : la clé vient de l'env. Aucun test ne fait de VRAI appel réseau (post_json injectable, pattern `trader/llm.py:448`).
- Vérification avant chaque commit : `uv run ruff check` puis `uv run pytest -q > /tmp/pt.log 2>&1; echo EXIT=$?` — EXIT=0 lu.
- Style : docstrings français, dataclasses frozen, helpers purs ; suivre les conventions de `trader/agent_tools.py` et `tests/test_agent_tools.py`.

---

### Task 1: Client embeddings (`trader/embeddings.py`)

**Files:** Create `trader/embeddings.py` ; Test `tests/test_embeddings.py`.

**Interfaces — Produces:**
```python
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536

def embed_texts(
    texts: list[str], *, api_key: str,
    model: str = DEFAULT_EMBEDDING_MODEL,
    base_url: str = "https://api.openai.com/v1",
    post_json=None,          # défaut : llm._post_json ; injectable pour les tests
    batch_size: int = 512,
) -> list[bytes]:            # un blob float32 little-endian (1536×4 octets) par texte
```
Comportement : batch par `batch_size`, POST `{base_url}/embeddings` payload `{"model", "input": [...]}`, header `Authorization: Bearer {api_key}` ; réponse `data[i].embedding` (l'ordre suit `index`) → `numpy.asarray(vec, dtype=np.float32).tobytes()`. Erreur HTTP/réseau → lever `RuntimeError` compacte (l'appelant décide ; dans l'outil, le contrat d'erreur standard l'absorbe).

**Steps (TDD)** : test avec `post_json` stub (2 textes → 2 blobs de 6144 octets, ordre préservé, batching vérifié avec batch_size=1 → 2 appels) → FAIL → implémenter → PASS → commit `feat(embeddings): client OpenAI minimal (pattern _post_json, zéro dépendance)`.

---

### Task 2: Store — schéma + ingestion idempotente (`trader/agent/learnings/store.py`)

**Files:** Create `trader/agent/learnings/store.py` ; Test `tests/test_learnings_store.py`.

**Interfaces — Produces:**
```python
class LearningsStore:
    def __init__(self, db_path: str | Path): ...   # crée schéma si absent (notes, FTS5 notes_fts, recalls)
    def ingest_jsonl(self, path: Path, *, source: str) -> dict   # {"inserted": n, "skipped": n}
    def count(self) -> int
```
Schéma table `notes` : colonnes du design §4.1 (id PK, decision_id UNIQUE, ts, symbol, family, venue, action, intent, executed, reason, note, concepts, source, valid_from, valid_until, superseded_by, verdict, forward_return, outcome_score, q_value, embedding BLOB). `family` = `trader.domain.semantic.catalog.family_for_symbol(symbol)` (None accepté ; ancien import `trader.semantic.catalog` conservé en compat). FTS5 `notes_fts(note, content='notes', content_rowid='id')` maintenue par triggers INSERT/UPDATE/DELETE. Table `recalls(id PK, decision_id TEXT, note_ids TEXT/*JSON*/, ts TEXT)`.

Ingestion : lit un JSONL (format de `state/archive/learnings-from-ledger.jsonl` : ts, symbol, note, action, intent, executed, reason, decision_id) ; dédup par `decision_id` (INSERT OR IGNORE) ; lignes sans decision_id : clé de secours `ts|symbol` (stockée comme decision_id synthétique `synth:{ts}|{symbol}`). `valid_from = ts`.

**Steps (TDD)** : tests — schéma créé ; ingestion d'un fixture 3 lignes → 3 inserted ; re-run → 0 inserted 3 skipped (idempotence) ; ligne sans decision_id → clé synthétique ; FTS5 répond à un MATCH. Commit `feat(learnings-store): schéma SQLite + ingestion idempotente des archives`.

---

### Task 3: Scoring par symbole — lift + shrinkage

**Files:** Modify `trader/agent/learnings/store.py` ; Test append `tests/test_learnings_store.py`.

**Interfaces — Produces:**
```python
    def apply_verdicts(self, bootstrap_json: Path) -> int
        # lit state/archive/learnings-outcome-bootstrap.json (format du script
        # scripts/learnings_outcome_bootstrap.py) et met à jour verdict/forward_return
        # par decision_id. Retourne le nombre de notes mises à jour.
    def compute_outcome_scores(self, *, shrinkage_k: float = 5.0) -> dict
        # base_rate par symbole = wins/(wins+losses) des notes scorables du symbole
        #   (≥5 scorables ; sinon fallback base_rate famille ; sinon globale)
        # lift(note) = (1.0 si WIN sinon 0.0) − base_rate    # notes WIN/LOSS seulement
        # outcome_score = n·lift/(n+k) avec n=1 en V1 (shrinkage vers 0)
        # NEUTRAL/UNKNOWN → outcome_score=0.0 (neutres au ranking, verdict visible)
        # retourne {"scored": n, "base_rates": {...extrait...}}
```

**Steps (TDD)** : fixtures avec 2 symboles à base rates opposées (A: 4W/1L, B: 1W/4L) + 1 symbole à 2 notes (fallback global) → vérifier : lift signé correct des deux côtés, shrinkage (|score| = |lift|/6 pour n=1, k=5), NEUTRAL→0. Commit `feat(learnings-store): scoring FLAIR par symbole (lift vs base rate + shrinkage)`.

---

### Task 4: Backfill embeddings + recherche hybride

**Files:** Modify `trader/agent/learnings/store.py` ; Test append.

**Interfaces — Produces:**
```python
    def backfill_embeddings(self, embedder: Callable[[list[str]], list[bytes]]) -> int
        # n'embedde QUE les notes sans embedding ; retourne le nombre embeddé
    def search(
        self, *, query_vec: bytes | None = None, text_query: str | None = None,
        symbol: str | None = None, family: str | None = None,
        limit: int = 8, now: datetime | None = None, tau_days: float = 30.0,
    ) -> list[dict]
        # 1) SQL facetté : symbol/family si fournis + valid_until IS NULL OR > now
        # 2) candidats : FTS5 bm25 (si text_query) ∪ cosine numpy (si query_vec,
        #    brute-force sur les embeddings du sous-ensemble facetté)
        #    sans query ni vec : tri direct par score final
        # 3) fusion RRF k=60 des deux classements
        # 4) score final = rrf + outcome_score + exp(−age_days/tau_days) − 1
        #    (chaque terme ∈ [−1,1] env. ; formule simple V1, calibrée en phase ③)
        # 5) retourne [{"id", "ts", "symbol", "verdict", "outcome_score", "note"(240c)}]
    def record_recall(self, *, decision_id: str, note_ids: list[int]) -> None
```

**Steps (TDD)** : embeddings backfill (2 sans, 1 avec → 2 embeddés) ; search facettes strictes (symbol filtre) ; note expirée (valid_until passé) exclue ; RRF stable (même entrée → même ordre) ; cosine trouve la note la plus proche d'un query_vec fixture ; sortie ≤ limit et notes tronquées 240c ; record_recall écrit la ligne. Commit `feat(learnings-store): embeddings backfill + recherche hybride facettes/FTS5/cosine/RRF`.

---

### Task 5: Outil `recall_learnings` dans le registre

**Files:** Modify `trader/agent_tools.py` (pattern des outils existants), `trader/codex_client.py` (`_TOOL_CATALOG` : 1 ligne) ; Tests append `tests/test_agent_tools.py`, `tests/test_codex_client.py`.

**Interfaces:**
- Consumes: `ToolContext` reçoit un nouveau champ `learnings_recall_provider: Callable[[dict], dict] | None = None` (même pattern que `position_risk_provider`).
- Produces: `TOOL_REGISTRY["recall_learnings"]`, args `{symbol?: str, family?: str, query?: str, limit?: int≤8}` — au moins un de symbol/family/query requis (validator). Handler : provider absent → `{"error": "unavailable"}` ; symbol fourni hors `allowed_symbols` → `symbol_not_allowed` (pattern get_position_risk) ; sinon retourne le payload du provider tel quel (le provider est responsable des bornes ; le handler re-tronque `rows` à 8 par défense).
- Catalogue prompt : `"- recall_learnings{symbol?|family?|query?, limit?} : mémoire vérifiée — notes passées pondérées par leurs résultats réels\n"`.

**Steps (TDD)** : suivre exactement le pattern des tests Task 4/11 du plan agent-tools (`tests/test_agent_tools.py` : provider stub, allowlist, validation args, provider absent) + test catalogue (`tests/test_codex_client.py::test_catalogue_prompt_expose_les_outils_semantiques` étendu). Commit `feat(agent-tools): recall_learnings — la mémoire outcome-weighted entre au registre`.

---

### Task 6: Câblage daemon — provider + trace des injections

**Files:** Modify `trader/daemon.py` (`_run_tool_round` et ses appelants) ; Test `tests/test_daemon_tool_round.py` (append).

**Comportement :**
1. Le daemon construit (paresseusement, une fois par run) un `LearningsStore(STATE_DIR / "learnings.db")` SI le fichier existe (sinon provider None → l'outil répond `unavailable` ; la création du .db reste le job du script d'ingestion, Task 7).
2. `learnings_recall_provider` = closure qui : embedde `args["query"]` via `embeddings.embed_texts` (clé `os.getenv("OPENAI_API_KEY")` ; pas de clé ou pas de query → `query_vec=None`, la recherche reste facettes+FTS5) puis appelle `store.search(...)` avec `limit=min(args.limit or 5, 8)`.
3. Trace des injections (design §4.4) : après une tournée, pour chaque décision du chunk, le daemon extrait des traces les calls `recall_learnings` outcome=ok et appelle `store.record_recall(decision_id=<decision_id de la row>, note_ids=<ids du result>)`. Le decision_id de la row est construit au moment de la persistance (`cycle_ts|sequence|symbol`) — passer par le même helper que `build_decision_row` (`decision_ledger._decision_id`, vérifier son nom exact ligne ~55).
4. AUCUN appel réseau pendant les tests : monkeypatch de `embed_texts`.

**Steps (TDD)** : flag on + tournée mockée contenant un call recall_learnings ok → `record_recall` appelé avec les bons note_ids (spy sur le store) ; pas de query → pas d'appel embedder ; store absent → outil unavailable sans crash. Commit `feat(daemon): provider recall_learnings + trace des notes injectées par décision`.

---

### Task 7: Script d'ingestion + config + doc

**Files:** Create `scripts/learnings_ingest.py` ; Modify `.env.example`, `Makefile`, design doc (statut).

**Comportement du script** (CLI, chargera `.env` via `llm.load_dotenv()`) :
1. `LearningsStore("state/learnings.db")` ; ingest des 3 sources si présentes : `state/archive/learnings-from-ledger.jsonl` (source=ledger-backfill), `state/archive/learnings-evicted.jsonl` (source=runtime), `state/learnings.jsonl` (source=runtime).
2. `apply_verdicts("state/archive/learnings-outcome-bootstrap.json")` (relancer d'abord `scripts/learnings_outcome_bootstrap.py` si l'utilisateur veut des verdicts frais — le mentionner dans l'aide).
3. `compute_outcome_scores()`.
4. `backfill_embeddings(...)` avec `OPENAI_API_KEY` (option `--no-embeddings` pour tout faire sauf ça).
5. Affiche le bilan (counts par étape) en JSON sur stdout.
- Makefile : cible `learnings-ingest:  ## Ingestion/scoring/embeddings du store de recall (state/learnings.db)`.
- `.env.example` : documenter `OPENAI_API_KEY` (embeddings recall).
- Design doc : Status → « V1 implémentée (store + outil + trace) ; phases ③ decay calibré/MemRL et ④ bench à venir ».

**Steps** : test léger du script (fonction main importable, run sur tmp state fixture avec --no-embeddings → db créée, counts corrects) ; validation complète ; commit `feat(learnings): script d'ingestion du store de recall + config`.

---

## Après le plan (hors tâches, orchestrateur)

- Exécuter l'ingestion RÉELLE (`make learnings-ingest`) — ~2 requêtes embeddings pour 2k notes.
- Redémarrer le daemon (fenêtre inter-cycle) pour activer le provider.
- Review Codex du chantier complet avant fermeture.
- Mesure : 24-48 h de `runtime.tool_calls` + table `recalls`.
