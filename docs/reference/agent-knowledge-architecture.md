# Référence — Architecture de connaissance de l'agent

> **Type** : Reference (Diátaxis) — cadre stable.
> **État vérifié** : 2026-08-20.
> **Rôle** : ranger toute connaissance de l'agent par sa **nature** (delta vs niveau,
> général vs par-nom, périssable vs stable), **pas** par son système technique. Boussole
> des chantiers learnings / macro / news / recall.

## Pourquoi ce document

Le contexte de décision empilait **trois mémoires de natures différentes** dans un seul
tuyau « learnings » (règles globales + notes figées par symbole + notes brutes récentes),
en parallèle d'un RAG dormant. Résultat : redondance, ciblage dans le mauvais système, et
des allers-retours de conception. Ce document pose la taxonomie stable pour arrêter de
re-empiler les bacs.

## Les 3 bacs de connaissance

| | **① Compétence** | **② Situation** | **③ Expérience** |
|---|---|---|---|
| **Question** | comment trader | que se passe-t-il sur ce nom | qu'ai-je vécu |
| **Granularité** | transversale | **par symbole** | par trade / setup |
| **Nature** | niveau (skill) | info + état | épisodique |
| **Durée de vie** | lente | **périssable** | cumulative |
| **Source** | mandat, guardrails, `global` | fil d'actu, macro, **analyste** | `learnings.db` + FLAIR |
| **Injection** | push permanent (cadrage) | brief borné à l'agent univers, puis tranche utile par symbole | retrieval borné au moment de décider + attribution/MemRL en coulisse |

- **① Compétence** — « comment trader » est **transversal** : gérer un stop, lire un régime,
  reconnaître un setup ne dépend pas du symbole. Il n'existe pas de « manière de trader AAPL »,
  seulement une manière de trader **un setup**.
- **② Situation** — le contexte **par nom** est **événementiel** : news, macro, géopolitique,
  catalyseurs. C'est la matière réellement spécifique-symbole.
- **③ Expérience** — la mémoire des trades passés, pondérée par le résultat (FLAIR) et
  par l'utilité de ses rappels (MemRL). Elle n'est jamais une règle permanente
  par nom : l'agent la demande explicitement avec `recall_learnings`, qui rend
  jusqu'à huit notes classées par appel, au moment précis de décider.

## Les 3 lois transverses

1. **Info ≠ État** (cœur de ②). Un event est un **delta** qui décroît en se digérant ; ce qui
   persiste, c'est son **effet sur l'état** (le narratif). → deux registres par nom :
   **catalyseurs forward** (à venir → rôle risque/timing) + **narratif backward** (état digéré
   des events passés). La « durée de vie » d'un event n'est pas fixe : il vaut tant qu'il n'est
   pas digéré.
2. **Pondération par impact, grossièrement.** Le code pondère (récence, surprise vs consensus,
   mouvement anormal vs vol normale) ; **le LLM juge l'anticipation** (« déjà pricé » vs « vraie
   surprise »). Le marché price en avance → l'impact « vrai » est inattribuable proprement ; on
   **assume l'imperfection**, ce n'est pas à résoudre en amont. *Le code fournit les faits, le
   LLM tranche l'interprétation* (« code over instructions »).
3. **Digestion.** Event chaud → digéré → fondu dans l'état. **L'analyste-news est le digesteur** :
   il transforme le flux d'events bruts (②) en un narratif/état par nom. Il ne
   sélectionne jamais le scope : le brief courant est consommé par l'agent
   univers, qui compose lui-même la hotlist. Le brief et la sélection portent le
   même `candidate_scope_id` ; aucun brief ancien n'est réutilisé sur un nouveau
   scope.

## Ce que le cadre tranche

- **③ n'est jamais stocké comme règle permanente par symbole.** Le détail reste
  pull via `recall_learnings`; FLAIR et MemRL travaillent ensuite en coulisse
  pour curer les règles globales plutôt que pousser une expérience historique
  au réveil d'un symbole.
- **① reste** (le `global` = compétence générale ; guardrails ; mandat).
- **Le `by_symbol` disparaît** du contexte : c'était ③ déguisé en ② — une « règle de trading par
  nom » qui n'existe pas. (`raw_recent` idem : ni ciblé, ni un état.)
- **② est le vrai chantier « par symbole »** : la situation événementielle, via **l'analyste-news**.

## Existe / manque

- **Existe** : ① (`global` + `mandate/guardrails.json` + mandat), ③
  (`learnings.db` + FLAIR + recall), ② matière brute (`news_items`, calendrier,
  séries) et digesteur async (`news_briefs`).
- **Existe comme dérivé sans retrieval** : `situation_memory.db`, index FTS5 des
  points de briefs. Le scoring FLAIR est actif : le sync daemon appelle
  `refresh_situation_outcomes()` en arrière-plan, fail-open ;
  `scripts/situation_note_analytics.py evaluate` reste le secours opérateur.
  Le retrieval n'est pas branché — aucun agent runtime n'appelle encore
  `search()`. Ce n'est donc pas un RAG de situation actif. Voir
  [`situation-memory.md`](situation-memory.md).
- **Existe aussi** : scopes candidats immuables, ledgers challenger/univers,
  projection préparée, activation pré-open avec fallback observable, et tranche
  de mandat active projetée par symbole au trader. D16 attribue désormais les
  sélections Univers contre le banc du scope et réinjecte un digest agrégé,
  agent-only, dans son prompt. La décision trader conserve son `mandate_ref`
  pour l'audit.
- **Manque encore** : le retrieval de situations historiques par l'agent Univers
  et MemRL situation (`q_value`), pas l'attribution D16.

## Implication pour les chantiers

1. **Chemin courant de ②** — les jointures scope → brief → run univers →
   activation sont persistées, puis seule la tranche de mandat utile est
   projetée au trader. Mesurer ce chemin présent avant d'activer un recall de
   situation historique.
2. **Nettoyage acté (③ → coulisse)** : retirer `by_symbol` + `raw_recent` du contexte, garder
   `global` + guardrails. Trivial, indépendant.
3. **③ (recall/FLAIR/MemRL)** : maintenance et attribution restent en
   **arrière-plan** ; `recall_learnings` rend au plus huit expériences par appel
   explicite. FLAIR et MemRL rerankent le RAG learnings. Pour ②, le futur
   retrieval vise d'abord l'agent univers et ne devient jamais un décideur.

## Voir aussi

- Spec superseded : [`../superpowers/specs/2026-07-03-learnings-recall-push-design.md`](../superpowers/specs/2026-07-03-learnings-recall-push-design.md)
- Contexte agent (le cockpit envoyé au LLM) : [`agent-context.md`](agent-context.md)
- RAG des learnings : [`learnings-rag.md`](learnings-rag.md)
- Mémoire de situation (bac ②) : [`situation-memory.md`](situation-memory.md)
- Fil d'actu / news : [`news.md`](news.md) · Macro : [`macro.md`](macro.md)
