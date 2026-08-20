# Tutoriel — Premier cycle paper

> **Type** : Tutorial (Diataxis).
> **But** : lancer un cycle sans ordre, puis lire ce qu'il a réellement fait.

## 1. Préparer un environnement sans effet

Depuis la racine du dépôt, synchronise les dépendances puis consulte le runbook
si la source de marché n'est pas déjà disponible :

```bash
uv sync
./run.sh --once
```

N'ajoute pas `--live` dans ce premier parcours. Le résultat utile est un cycle
qui produit un rapport, un diagnostic de données ou un HOLD infrastructurel ;
ce n'est pas forcément une décision LLM ni un trade.

## 2. Lire le résultat du cycle

Après la fin du processus, inspecte les surfaces de lecture sans les éditer :

```bash
casys-trader status --json
tail -n 1 state/last_report.json
tail -n 5 state/decisions.jsonl
```

Si un fichier est absent, le cycle n'a peut-être pas atteint l'étape qui le
produit. Lis les logs et le [runbook](../how-to/run-the-daemon.md) avant de
relancer ; ne fabrique pas de ledger à la main.

## 3. Distinguer les trois résultats possibles

| Résultat | Comment le reconnaître | Signification |
|---|---|---|
| Décision LLM | `model_called=true`, `decision_source="llm"` | le modèle a retourné une décision structurée |
| HOLD infra | `model_called=false`, `decision_source="infra"` | un gate technique a arrêté le travail sans appeler le modèle |
| Plan armé | `decision_source="armed_plan"` | le code a résolu un plan déterministe sur données fraîches |

Un `HOLD` seul ne prouve donc pas l'absence ou la présence d'un appel LLM. La
provenance est la donnée décisive.

## 4. Continuer sans élargir le risque

Le cycle est lisible et son origine est comprise : poursuis avec
[la preuve d'une décision](trace-a-paper-decision.md). Pour comprendre les
gates et la queue avant un essai paper exécutant, lis le
[cycle de décision](../explanation/architecture/decision-cycle.md) et la
[référence d'exécution](../reference/execution.md).
