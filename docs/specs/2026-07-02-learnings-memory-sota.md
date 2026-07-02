# SOTA mémoire/RAG pour les learnings de l'agent — synthèse de recherche

**Date** : 2026-07-02
**Statut** : cadrage du chantier [[learnings-rag]] — 4 recherches web parallèles + synthèse
**Contexte** : corpus 1 867 notes datées (+~78/j), outcome vérifiable par decision_id,
retrieval via tool call en cycle live (<1 s), temporalité critique (régimes).

## Synthèse architecture — mémoire de learnings trading

---

### 1. Paysage 2025-2026 en 5 lignes

La convergence majeure est un consensus anti-infrastructure : à l'échelle agent-memory (<10 M entrées), les vector DBs dédiées, les graphes de connaissances LLM-construits et les community detections (GraphRAG-Microsoft) sont tous sur-dimensionnés. LazyGraphRAG a démontré en nov. 2024 que la valeur de GraphRAG réside dans le raisonnement à query-time, pas dans le graphe pré-calculé — Microsoft lui-même a abandonné son propre paradigme lourd. Parallèlement, SQLite+FTS5+vecteur bat ChromaDB/Pinecone à 0.3 ms sur 100k entrées. Le maillon manquant — connecter outcome connu → signal de retrieval futur — a été comblé par MemRL (arXiv 2601.03192, jan. 2026) avec un Q-value overlay non-paramétrique. Côté embeddings locaux, Qwen3-Embedding-0.6B (Alibaba, mai 2026, 64.33 MTEB multilingual) détrône BGE-M3 comme default local FR/EN sous 1B params.

---

### 2. Les 3 architectures candidates, classées

#### #1 — SQLite hybride + Q-value MemRL (RECOMMANDÉE)

**Principe :** Une base SQLite avec trois couches fusionnées par RRF : FTS5 (BM25 sur texte libre), colonne embedding (brute-force cosine, <0.1 ms sur 2k notes), filtre SQL pré-retrieval sur les métadonnées structurées. Q-value scalaire stocké dans une colonne adjacente, mis à jour asynchrone depuis l'outcome forward. Score final = `(1−λ)·sim_cosine + λ·Q_i · exp(−Δt/τ)`.

**Pourquoi #1 :** Le corpus de 2k notes (+80/j) est exactement dans la plage où SQLite brute-force cosine est plus rapide que tout index ANN (< 5 ms total, sans overhead de serveur). Les entités (symbol, family, outcome, decision_id) *étant déjà en SQL*, l'extraction LLM d'entités est du gaspillage pur — les findings convergent tous sur ce point (arXiv 2506.02404, ZeroClaw). Le Q-value MemRL se branche directement sur le champ `win/loss` existant sans modifier aucun poids de modèle.

**Risques :** Pas de multi-hop cross-symboles natif. Pour « quels symboles corrélés à TSMC ont montré le même breakout ? », il faudrait une requête SQL explicite ou une mini-couche graphe additionnelle.

---

#### #2 — LanceDB embedded + bm25s + RRF + cross-encoder léger

**Principe :** LanceDB (embedded, disk-based, hybrid BM25+dense+SQL filter natif en 1 appel) + bm25s (100–500× plus rapide que rank_bm25) + RRF k=60 + cross-encoder `ms-marco-MiniLM-L-6-v2` (< 6 MB, ~150 ms CPU sur top-20). Budget latence mesuré : ~200–300 ms.

**Pourquoi #2 et non #1 :** Gagne en expressivité hybrid search (BM25 natif intégré, versioning automatique pour auditer les learnings par régime) mais ajoute une dépendance Rust (binaire natif LanceDB) et un processus cross-encoder supplémentaire. Meilleur choix si le pipeline grossit (passage transparent à IVF-PQ index à 10k notes sans changement de code). Moins déterministe que #1 à cause du cross-encoder (mais reste < 1 s).

**Avantage différenciant :** Le reranker cross-encoder peut être fine-tuné sur les paires `(situation_courante, learning_win)` vs `(situation_courante, learning_loss)` — gain NDCG@10 de +17 % mesuré, plus rentable que fine-tuner l'embedding à ce stade.

---

#### #3 — HippoRAG v2 alimenté depuis SQL + retrieval PPR

**Principe :** Graphe alimenté directement depuis le schéma SQL (symbol, family, decision_id, outcome = nœuds et arêtes), sans extracteur NLP/LLM. Retrieval par Personalized PageRank (PPR) depuis la requête : quelques ms sur 2k nœuds, déterministe.

**Pourquoi #3 :** Pertinent uniquement si le besoin de multi-hop associatif cross-symboles devient fréquent (« tous les learnings sur semi-conducteurs TW corréléd avec un context macro précis »). L'extraction OpenIE/LLM peut être court-circuitée en injectant les métadonnées SQL directement — les findings confirment que c'est supporté. PPR < 1 ms sur 2k nœuds.

**Limite :** Surcouche de maintenance (graphe NetworkX ou Neo4j) pour un cas qui reste marginal dans le cycle de décision live. À considérer si le corpus dépasse 5k notes et que les questions cross-symboles deviennent un vrai besoin.

---

#### Pourquoi GraphRAG-Microsoft et RAG vanilla sont exclus

**GraphRAG-Microsoft :** $20–40 d'indexation pour 1M tokens, reconstruction quasi-totale à chaque ajout, latence > 1 s au Global Search, communautés Leiden sans sens sur 2k notes courtes. Sur-dimensionné par un facteur 100. Microsoft lui-même a convergé vers LazyGraphRAG qui abandonne le graphe pré-calculé.

**RAG vectoriel vanilla :** Le seul axe où il excelle — les questions factuelles ponctuelles — est couvert par FTS5+filtre SQL à moindre coût. Il rate complètement la décroissance temporelle (learning profitable en bull 2024 = toxique en bear 2025) et ne pondère pas les outcomes. Sur un corpus à régimes changeants, c'est un défaut structurel, pas un manque de tuning.

---

### 3. Point différenciant : exploiter la vérité terrain

**Ce qui a été fait dans la littérature :**

- **MemRL** (arXiv 2601.03192, code fonctionnel) : Q-value overlay non-paramétrique. `Q_new = Q_old + α(r − Q_old)` où `r = win/loss` depuis le `decision_id`. Pearson r = 0.861 entre Q-values et taux de succès réels sur 2000 décisions. Premier système à connecter outcome → signal de retrieval *futur* sans toucher les poids du modèle. Applicable directement.

- **FLAIR** (arXiv 2508.13390, déployé en prod chez Microsoft/Copilot) : `score = α·cosine + (1−α)·outcome_score_i` où `outcome_score` est simplement `(nb_wins − nb_losses) / nb_apparitions`. 30 lignes Python. Bootstrap immédiat depuis les 2000 décisions historiques.

- **NUDGE** (arXiv 2409.02343, ICLR 2025) : adaptation offline des embeddings — résout le problème d'optimisation contraint qui rapproche les embeddings de notes des requêtes pour lesquelles elles ont précédé un WIN. 10–15 % de gain NDCG@10, tourne en quelques minutes CPU. Batch hebdomadaire.

- **ExpeL-style** (arXiv 2308.10144, AAAI 2024) : tag `validated/invalidated` directement sur la note depuis son `decision_id` d'origine. Lien causal propre (chaque note appartient à une trajectoire). Au retrieval : boost ou filtre selon le tag.

**Ce qui n'a pas été fait :** L'attribution causale dans un contexte multi-note (5 learnings injectés pour un trade, un seul outcome) reste le trou de recherche confirmé par tous les papiers. Toutes les approches font une hypothèse de contribution uniforme. C'est le vrai gap pour ce cas d'usage précis.

**Approche la plus défendable ici :** Démarrer par FLAIR (trivial à bootstrapper sur les 2000 notes existantes) + decay temporel conditionné par le régime. Passer à MemRL dès que le pipeline live tourne (update asynchrone post-outcome, correction lag mesuré explicitement — insight PatchRAG arXiv 2604.06647).

---

### 4. Stack concret recommandé

| Couche | Choix | Justification |
|--------|-------|---------------|
| Stockage | SQLite unique (FTS5 + colonne vecteur BLOB) | 0 dépendance, 1 fichier, SQL natif sur métadonnées existantes |
| Embeddings | Qwen3-Embedding-0.6B via Ollama | 639 MB, FR natif, Matryoshka (256 dims = 3× moins de RAM), pré-calculé offline |
| BM25 | bm25s en RAM | 100–500× plus rapide que rank_bm25, scipy only, 2k docs = quelques MB |
| Fusion | RRF k=60 (5 lignes) | Aucun hyperparamètre à tuner |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` (< 6 MB) | 150 ms CPU sur top-20, fine-tunable sur paires win/loss |
| Outcome weighting | FLAIR outcome_score (col SQL) → MemRL Q-value | Bootstrap FLAIR immédiat, migration MemRL sans refonte |
| Decay temporel | `f(t) = Q_i · exp(−Δt/τ)`, τ par régime (tag SQL) | Déterministe, auditabe, un job quotidien |
| Consolidation offline | SCM-NREM inspiré : script nocturne qui regroupe N notes identiques en 1 méta-note | Réduit la redondance, maintient l'audit trail des notes sources |

**Budget latence live :** BM25 ~5 ms + cosine brute-force ~1 ms + RRF < 1 ms + reranker top-20 ~150 ms + overhead query embedding (Qwen3 via Ollama) ~50 ms = **~210 ms total, très confortablement sous 1 s.**

**Coût de maintenance :** 3 dépendances pip (`lancedb` ou `bm25s` + `sentence-transformers` + `ollama`), 1 fichier SQLite, 0 serveur externe.

---

### 5. Ce qu'on ne sait pas / à prototyper d'abord

**Inconnu #1 — Attribution multi-note** : Si 5 learnings sont injectés pour un trade gagnant, lequel a vraiment contribué ? Toutes les approches imputent uniformément. Sans log explicite des notes effectivement injectées par `decision_id`, le signal Q-value sera bruité. **Prototyper en premier** : logger systématiquement les IDs des notes injectées dans chaque contexte de décision, mesurer la corrélation Q-value/win_rate réel après 4 semaines.

**Inconnu #2 — Calibration de τ par régime** : Le decay exponentiel `exp(−Δt/τ)` avec quel τ ? Sur des données historiques insuffisantes, τ est une supposition. **Prototyper** : comparer un τ=30j (conservateur) vs τ=7j (agressif) sur un holdout temporel.

**Inconnu #3 — Gain réel du BM25 sur ce corpus** : Les tickers (ASML, TSMC) sont rares dans les learnings — BM25 pourrait sur-performer le dense sur les lookups exacts, ou sous-performer si les learnings sont paraphrasés. **Mesurer** : ablation BM25-seul vs dense-seul vs RRF sur 200 requêtes synthétiques issues des `decision_id` existants.

**Inconnu #4 — ROI du fine-tuning à 2k exemples** : La littérature prédit +3–5 % NDCG@10 sur dims pleines à ce volume (gains significatifs à partir de 5–10k). À ~7k notes (dans 2 mois au rythme actuel), relancer l'évaluation. Ne pas fine-tuner maintenant : la baseline FLAIR + decay est plus rapide à déployer et à mesurer.

**Inconnu #5 — MemoryArena warning** : Les benchmarks standards (LoCoMo, LongMemEval) surestiment massivement les gains (~100 % sur benchmark → 40–60 % en agentique réel). **Ne calibrer que sur les trades réels** via `decision_id`, jamais sur un benchmark générique.

---

**Sources clés :** arXiv 2601.03192 (MemRL), arXiv 2508.13390 (FLAIR), arXiv 2409.02343 (NUDGE), arXiv 2604.06647 (PatchRAG/correction lag), arXiv 2501.13956 (Graphiti/bi-temporel), ZeroClaw SQLite hybrid benchmark, Qwen3-Embedding-0.6B HuggingFace, bm25s.github.io, arXiv 2506.02404 (GraphRAG-Bench — confirme l'overhead LLM-entity sur corpus structurés).