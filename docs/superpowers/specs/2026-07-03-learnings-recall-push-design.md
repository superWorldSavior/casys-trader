# Learnings recall en PUSH — remplacer le digest ciblé par un recall sémantique

> **⚠️ SUPERSEDED (2026-07-04) par [`docs/reference/agent-knowledge-architecture.md`](../../reference/agent-knowledge-architecture.md).**
> Le **diagnostic** de ce document reste valide (§1 : deux systèmes de learnings en parallèle,
> l'agent ne pull jamais, `by_symbol` figé). Mais sa **solution** — pousser le recall de learnings
> **par symbole** — est **écartée** par le cadre d'architecture : la mémoire d'**Expérience** (③,
> learnings) ne se pousse pas par symbole, elle travaille en **coulisse** (distiller le `global`,
> l'attribution, MemRL) ; le vrai push par-symbole = la **Situation** événementielle (②, via
> l'analyste-news). **Acté** : retrait du `by_symbol`/`raw_recent` du contexte (③ déguisé en ②).
> Conservé pour trace.
>
> **Type** : Spec (design — cadrage).
> **Date** : 2026-07-03 · **Statut** : 🗄️ SUPERSEDED (voir bandeau).
> **Code concerné** : `trader/runtime/daemon.py` (contexte de décision, `_build_recall_provider`),
> `trader/learnings/store.py` (FLAIR/MemRL), `trader/learnings/consolidator.py` (digest).
> **Dépend de** : task-ledger (1 appel/symbole) pour le ciblage optimal.

## 1. Problème — deux systèmes de learnings en parallèle

À chaque décision, le daemon POUSSE déjà (`daemon.py:1939`, ~1k tokens) :
- **10 règles globales** « AGIR: » (consolidateur LLM, génériques) ;
- **`by_symbol`** : ~5 notes, **5 symboles seulement** (figé au digest) ;
- **10 bruts récents** (`raw_recent`, pas liés au symbole décidé).

En parallèle, le **RAG** (`learnings.db`, 2091 notes, embeddings, FLAIR) expose
`recall_learnings` en **pull** (tournée read-only). Or l'agent **ne pull jamais**
(0 tournée, 0 recall tracé — comme `context_request`, le push lui suffit).

Conséquences :
- Le **ciblage vit dans le mauvais système** : un `by_symbol` figé à 5 entrées côté
  push, pendant que le vrai moteur de ciblage (recall sémantique) dort.
- **Redondance** : deux sources de learnings, l'agent ne sait pas laquelle prime.
- Le RAG **ne prouve jamais sa valeur** (impossible d'isoler son apport).
- **MemRL bloquée** : la phase RL (`q_value`) attend du volume de recalls qui
  n'arrivera jamais en pull.

## 2. Design — un seul mécanisme de ciblage

Garder le global (cadrage), remplacer la partie ciblée par un recall poussé :

| Bloc du contexte | Avant | Après |
|---|---|---|
| Méta-stratégie | 10 règles globales | **inchangé** (cadrage) |
| Ciblage symbole | `by_symbol` figé (5) + `raw_recent` (10) | **recall sémantique poussé** : top-k notes proches du **setup courant** |

Mécanique :
1. Pour le symbole décidé, construire une **query de setup** depuis le contexte déjà
   calculé (famille, RS, régime, indicateurs saillants, action envisagée).
2. `embed(query)` → recherche top-k dans `learnings.db`, **pondérée FLAIR**
   (`outcome_score`), avec repli FTS5 si l'embedding échoue (déjà géré,
   `daemon.py:1554`).
3. **Injecter** les k notes dans le contexte, à la place de `by_symbol`/`raw_recent`.
4. **Tracer** chaque recall dans `recalls` (`note_ids × decision_id`) → alimente le
   volume pour **MemRL (phase 3)**.

Le provider existe déjà (`_build_recall_provider`) — le chantier le **rebranche du
pull vers le push** dans la construction du contexte.

## 3. Pondération

- **Maintenant : FLAIR v1** — `outcome_score = lift/(1+shrinkage_k)`, lift = win vs
  base-rate par symbole, shrinkage bayésien vers 0. Suffisant pour ranker le top-k.
- **Ensuite : MemRL (phase 3)** — `q_value` appris sur l'impact réel des recalls,
  **débloqué par le volume** que ce push génère (cercle vertueux).

## 4. Dépendance & séquencement

- **Optimal avec le task-ledger** (1 appel = 1 symbole) : contexte dédié, recall
  parfaitement ciblé. Cf. chantier task-ledger (Lots B/C).
- **Prototypable avant** : en batch (5 symboles/appel), pousser **un bloc recall par
  symbole du batch** (chaque symbole a déjà son sous-contexte). Moins net mais
  déjà supérieur au `by_symbol` figé. → permet de démarrer la mesure sans attendre.

## 5. Invariants & mesure

- **Un seul mécanisme de ciblage** : `by_symbol`/`raw_recent` retirés du push une
  fois le recall branché (pas de coexistence).
- **Budget contexte borné** : k notes (≈ ce que coûtait `by_symbol`+`raw_recent`),
  pas d'inflation de tokens.
- **Fail-safe** : recall vide / embed KO → le global seul reste (jamais de contexte
  cassé).
- **A/B mesurable** : `global-seul` vs `global + recall`, comparé sur le
  `forward_return` des décisions (rejoint le bench A/B du chantier learnings-rag).
- **Traçabilité** : `recalls` non vide = preuve que le RAG est enfin exercé.

## 6. Hors scope

- L'écriture (`record_learning`) et la consolidation batch (`learnings_consolidated`)
  — chantier séparé (la consolidation reste fragile, cf. mémoire).
- L'implémentation MemRL elle-même (phase 3, une fois le volume de recalls acquis).
