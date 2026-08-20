# Architecture — cycle de décision

> **Type** : Explanation (Diataxis). Retour à l'[index Explanation](../README.md).

## Du réveil à la décision

```text
scheduler / indicator watch
  -> snapshot marché et fraîcheur
  -> sorties et veilles déterministes
  -> scope des symboles dus
  -> contexte partagé puis projection par symbole
  -> holds infra et plans armés
  -> queue decide ou compatibilité batch
  -> admission de risque, persistance et prochain réveil
```

Le cycle ne promet pas un appel LLM pour chaque symbole. Le scheduler sélectionne
les symboles dus ; le snapshot vérifie les barres, FX et l'éligibilité. Une
fraîcheur insuffisante ou le quiet gate génère un `HOLD` de provenance `infra`
sans appel modèle. Cette distinction est essentielle pour diagnostiquer un
« no-trade ».

## Contexte, pertinence et plans armés

Le cockpit partagé rassemble portefeuille, régime, fraîcheur et capacités. La
queue en projette une tranche `decision_focus_v1` par symbole, afin que le LLM
dispose de faits utiles sans recevoir le monde entier. Les détails sont
accessibles par outils read-only pendant la tournée bornée.

Avant le LLM, deux étages peuvent arrêter ou résoudre le travail :

1. le gate de pertinence évite un appel sans valeur attendue ;
2. un plan armé `EXECUTE_ORDER`, vérifié sur des données fraîches, peut être
   exécuté mécaniquement sans nouvelle décision agent.

Les décisions LLM restantes passent par une tâche durable par symbole lorsque
la queue est activée ; le mode batch est une compatibilité, pas une seconde
source d'autorité. [TaskLedger](../../reference/task-queue.md) décrit leases,
idempotence et retries.

## Validation avant l'effet

Une proposition d'entrée porte une `TradePlanEvaluation` liée au cycle et au
candidat. Le code revalide cette identité, le stop, les frais, le risque, la
capacité et les limites d'exposition juste avant le broker. La décision peut
donc être valide comme raisonnement mais refusée comme ordre.

Après chaque résultat, le recorder sépare `decision_source=llm`, `infra` ou
`armed_plan`, persiste l'audit et planifie le réveil suivant. Voir
[risk gate](../../reference/risk-gate.md), [exécution](../../reference/execution.md)
et [réveils](../../reference/wake-scheduler.md).
