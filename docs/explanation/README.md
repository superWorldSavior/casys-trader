# Explications d'architecture

> **Type** : Explanation (Diataxis).

Ces pages expliquent les choix, les frontières et les relations entre les
composants. Elles ne remplacent pas les contrats runtime : pour les invariants,
formats et commandes, suivre les pages [Reference](../reference/README.md).

| Page | Question traitée |
|---|---|
| [Vue d'ensemble](architecture/overview.md) | Quels sont les rôles et les frontières du système ? |
| [Cycle de décision](architecture/decision-cycle.md) | Comment un symbole passe-t-il du réveil à une décision vérifiée ? |
| [Exécution et état](architecture/execution-state.md) | Comment les ordres, sorties, veilles et preuves persistent-ils ? |
| [Intelligence et apprentissage](architecture/intelligence-learning.md) | Comment l'univers, les briefs et les learnings se distinguent-ils ? |
| [Trace Brain des décisions Trader](architecture/brain-trade-trace.md) | Quelles identités et provenances sont persistées sans reconstruction ? |
| [World model shadow](architecture/world-model-shadow.md) | Comment apprend-on une dynamique de marché (V1 + pilote cohorte) sans confondre action et état du monde ? |
| [World context / ontologie temporelle](architecture/world-context-ontology.md) | Qu'est-ce qui était connu, de quelle source, au cutoff — sans claim causal, sans NetworkX autorité ? |
| [Opérations et gouvernance](architecture/operations-governance.md) | Comment le LLM, les outils et le pilote de processus restent-ils bornés ? |

La façade historique [architecture.md](../architecture.md) conserve les titres
§1–14 et dirige vers ces pages. Les décisions datées restent dans le
[registre métier](../decisions/registre-decisions-metier.md).
