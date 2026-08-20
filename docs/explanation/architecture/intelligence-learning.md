# Architecture — intelligence et apprentissage

> **Type** : Explanation (Diataxis). Retour à l'[index Explanation](../README.md).

## Univers : sélectionner l'attention, pas trader

Le radar quantitatif et le scout news construisent un scope candidat immuable.
L'analyste macro/news le digère en brief ; l'agent Univers est le seul à
composer la hotlist, ensuite revalidée et activée par le code. Les sticky sont
ajoutés hors quota et une baseline déterministe protège l'indisponibilité de
l'agent.

D16 rend l'apprentissage de cette allocation concret : les sélections sont
jugées contre le banc du même scope (`allocation`) et, lorsqu'applicable, selon
leur direction. Le feedback agrégé, agent-only et à seuil minimal, est injecté
dans le prompt Univers ; la baseline reste séparée. Ce n'est ni une décision du
trader ni une autorisation de modifier la hotlist hors activation.

Voir [gestion d'univers](../../reference/universe-rotation.md) et
[pipeline Univers → Trader](../../reference/universe-trader-pipeline.md).

## Trois mémoires, trois rôles

| Bac | Contenu | Usage |
|---|---|---|
| Compétence | mandat, guardrails et règles globales | cadrage permanent |
| Situation | news/macro digérées par symbole, famille ou zone | brief frais et feedback comparatif |
| Expérience | outcomes de trades et learnings | recall explicite, FLAIR et MemRL |

La mémoire de situation est un index FTS dérivé des briefs. Son scoring de
marché est maintenu en arrière-plan par le sync daemon, fail-open et
désactivable ; le script reste un outil de secours. Son retrieval historique
n'est pas encore appelé par un agent runtime. À l'inverse, `recall_learnings`
peut ramener des expériences au moment de décider.

Ces frontières empêchent de transformer une note périssable par symbole en
« règle » permanente. Les contrats précis sont dans
[architecture de connaissance](../../reference/agent-knowledge-architecture.md),
[news](../../reference/news.md) et [learnings/RAG](../../reference/learnings-rag.md).
