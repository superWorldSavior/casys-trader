# Référence — Architecture de connaissance de l'agent

> **Type** : Reference (Diátaxis) — cadre stable.
> **Date** : 2026-07-04.
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
| **Injection** | push permanent (cadrage) | **push par symbole** | **coulisse** (nourrit ①, attribution, MemRL) |

- **① Compétence** — « comment trader » est **transversal** : gérer un stop, lire un régime,
  reconnaître un setup ne dépend pas du symbole. Il n'existe pas de « manière de trader AAPL »,
  seulement une manière de trader **un setup**.
- **② Situation** — le contexte **par nom** est **événementiel** : news, macro, géopolitique,
  catalyseurs. C'est la matière réellement spécifique-symbole.
- **③ Expérience** — la mémoire des trades passés, pondérée par le résultat (FLAIR). Faible
  volume par nom → sa valeur est en **agrégé** (distiller ①) et en **attribution**, pas en push
  par symbole.

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
   il transforme le flux d'events bruts (②) en un narratif/état par nom.

## Ce que le cadre tranche

- **③ n'est jamais poussé par symbole.** Il travaille en coulisse : distiller ① (le `global`),
  l'attribution, MemRL. → le **recall-push de learnings**
  ([`../superpowers/specs/2026-07-03-learnings-recall-push-design.md`](../superpowers/specs/2026-07-03-learnings-recall-push-design.md))
  est **déprioritisé** (diagnostic juste, solution écartée).
- **① reste** (le `global` = compétence générale ; guardrails ; mandat).
- **Le `by_symbol` disparaît** du contexte : c'était ③ déguisé en ② — une « règle de trading par
  nom » qui n'existe pas. (`raw_recent` idem : ni ciblé, ni un état.)
- **② est le vrai chantier « par symbole »** : la situation événementielle, via **l'analyste-news**.

## Existe / manque

- **Existe** : ① (`global` + `mandate/guardrails.json` + mandat), ③ (`learnings.db` + FLAIR +
  recall), ② **matière brute** (le fil d'actu persiste déjà les news ; collecte macro P1a en cours).
- **Manque** : ② le **digesteur** (analyste-news) + le **push situation** par symbole (récupération
  + pondération impact + injection dans le contexte du nom).

## Implication pour les chantiers

1. **Prochain chantier concret = l'analyste-news (②)** — transformer le flux d'events en état
   digéré par nom, puis le pousser. Pas le recall-push.
2. **Nettoyage acté (③ → coulisse)** : retirer `by_symbol` + `raw_recent` du contexte, garder
   `global` + guardrails. Trivial, indépendant.
3. **③ (recall/FLAIR/MemRL)** : reste vivant mais **en arrière-plan** (distillation du `global`,
   attribution). Réactivable en push plus tard si le volume le justifie — non prioritaire.

## Voir aussi

- Spec superseded : [`../superpowers/specs/2026-07-03-learnings-recall-push-design.md`](../superpowers/specs/2026-07-03-learnings-recall-push-design.md)
- Contexte agent (le cockpit envoyé au LLM) : [`agent-context.md`](agent-context.md)
- RAG des learnings : [`learnings-rag.md`](learnings-rag.md)
- Fil d'actu / news : [`news.md`](news.md) · Macro : [`macro.md`](macro.md)
