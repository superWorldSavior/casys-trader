# Décisions métier — guide de lecture

> **Type** : Explanation / carte des décisions (Diátaxis).
> **Source historique autoritative** :
> [`decisions/registre-decisions-metier.md`](decisions/registre-decisions-metier.md).
> **Comportement actuel** : pages [`reference/`](reference/README.md) et code.

Cette page explique comment lire les décisions D1 à D15 sans transformer leur
contexte historique en documentation runtime. Les chiffres, modèles, chemins de
modules et états d'implémentation consignés dans le registre restent datés du
jour de la décision. Lorsqu'un détail a évolué, la page Reference du sous-système
prime.

## Doctrine qui reste structurante

1. **Le LLM planifie, le daemon possède les effets.** L'agent produit des
   décisions, plans et veilles structurés. Le code calcule les faits, applique
   les gates, persiste les effets et relit les preuves.
2. **Le risque est déterministe.** Une décision LLM, un plan armé ou une sortie
   planifiée repasse par les contrôles d'exécution et de portefeuille appropriés.
3. **L'univers est une surveillance active.** Radar et news construisent un pool
   candidat ; l'agent Univers choisit la hotlist régionale ; les sticky sont
   ajoutés hors quota ; le trader conserve timing, taille et exécution.
4. **La comptabilité est en USD, les prix restent natifs.** Les conversions
   servent au sizing, aux limites et au reporting, jamais à déformer les barres
   ou niveaux de marché.
5. **L'expérience est évaluée, pas injectée en bloc.** Les rationales LLM et
   notes sont scorées après résultat ; les règles globales consolidées sont
   poussées, les expériences détaillées sont rappelées à la demande.

## Carte D1 à D15

| Décision | Sujet | Lecture actuelle |
|---|---|---|
| D1 | Séparer une opportunité manquée d'un mauvais trade | Le vocabulaire d'audit reste distinct ; voir [reporting](reference/reporting.md). |
| D2 | Régime cross-asset par famille | Signal de contexte, jamais autorité d'ordre ; voir [régime](reference/regime.md). |
| D3 | Réveil sur cluster de famille | Principe validé historiquement ; le registre porte ses points ouverts. |
| D4 | RVOL et expansion ATR dans le contexte | Principe validé ; le contexte courant est décrit dans [agent-context](reference/agent-context.md) et [agent-tools](reference/agent-tools.md). |
| D5 | News et qualification de surprise | La proposition temps réel initiale reste historique ; le pipeline best-effort livré relève surtout de D15 et de [news](reference/news.md). |
| D6 | Boucle de learnings auto-renforçante | L'implémentation a depuis évolué vers capture automatique, FLAIR, MemRL, recall et curation ; voir [learnings](reference/learnings-rag.md). |
| D7 | LLM planificateur plutôt qu'opérateur de polling | Réveils, quiet gate, plans armés et file grain-symbole sont actifs ; voir [scheduler](reference/wake-scheduler.md) et [queue](reference/task-queue.md). |
| D8 | Replay mécanique puis évaluation forward | Décision de mesure, sans confondre replay et vérité live ; voir le [how-to mesure](how-to/measure-and-replay.md). |
| D9 | Radar large et rotation du hot-set | Le radar et la baseline déterministe restent ; D15 remplace le cap et la propriété finale de la hotlist. |
| D10 | Hotlists par venue et univers actif | TW/EU/US sont composées selon les sessions, avec sticky hors quota ; voir [rotation](reference/universe-rotation.md). |
| D11 | Stops adaptatifs résolus au tir | Les intentions paramétriques et leur validation actuelle sont dans [exécution](reference/execution.md) et [outils agent](reference/agent-tools.md). |
| D12 | Préflight LLM des plans swing | Non retenu en l'absence de mesure démontrant sa nécessité ; le plan armé reste mécanique et gaté. |
| D13 | Surveillance swing-aware et sélection Univers | Le pré-open et l'analyse des venues fermées restent ; D15 remplace l'ancien contrat de candidats et d'override. |
| D14 | Comptabilité USD et sizing natif | Actif ; voir [FX](reference/fx.md) et [exécution](reference/execution.md). |
| D15 | Pool news-aware et agent Univers propriétaire de la hotlist | Contrat courant du pipeline régional ; voir [rotation](reference/universe-rotation.md), [news](reference/news.md) et [pipeline Univers → Trader](reference/universe-trader-pipeline.md). |

## Supersessions à connaître

- D15 ne supprime pas D9/D10/D13 : il conserve radar, baseline, venues,
  pré-open, hystérésis et sticky, mais remplace les anciens caps et la propriété
  nominale « défaut + override » de la hotlist.
- D12 documente une option explicitement écartée tant qu'une mesure ne justifie
  pas sa réouverture.
- Les étapes techniques décrites en D6 sont un jalon historique. Elles ne
  décrivent plus à elles seules le store de recall ni la consolidation courante.
- Les volumes d'appels et coûts cités dans D7 sont des données de motivation,
  pas un budget runtime actuel.

## Où vérifier le comportement aujourd'hui

| Question | Référence courante |
|---|---|
| Quand l'agent est-il réveillé ? | [Scheduler, réveils et watches](reference/wake-scheduler.md) |
| Comment décide-t-il et utilise-t-il ses outils ? | [File durable](reference/task-queue.md), [outils agent](reference/agent-tools.md) |
| Qui choisit les symboles ? | [Rotation d'univers](reference/universe-rotation.md) |
| Comment Univers influence-t-il le trader ? | [Pipeline Univers → Trader](reference/universe-trader-pipeline.md) |
| Quelles barrières s'appliquent aux ordres ? | [Risk gate](reference/risk-gate.md), [exécution](reference/execution.md) |
| Comment l'expérience devient-elle un learning ? | [Learnings & RAG](reference/learnings-rag.md) |
| Où se trouve la preuve décision → effet ? | [Gouvernance du processus](reference/process-governance.md), [reporting](reference/reporting.md) |

Pour modifier une orientation produit, ajouter ou amender explicitement une
décision dans le registre. Pour corriger un détail d'implémentation, mettre à
jour la page Reference correspondante sans réécrire l'histoire.
