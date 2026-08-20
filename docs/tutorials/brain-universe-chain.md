# Tutoriel — Chaîne Brain → Univers → Trader

> **Type** : Tutorial (Diataxis).
> **But** : suivre qui sélectionne l'attention, qui décide un ordre et où le
> feedback D16 revient, sans confondre ces rôles.

## 1. Partir du scope candidat

Le radar et le scout news construisent un scope immuable. L'agent Univers reçoit
ce scope et un brief frais : il compose une hotlist, mais ne décide ni un ordre
ni une taille de position. Lis d'abord le flux dans la
[référence Univers](../reference/universe-rotation.md).

```text
radar + scout -> scope candidat -> brief macro/news -> agent Univers
  -> hotlist préparée -> activation validée par le code -> univers trader
```

## 2. Lire la frontière d'autorité

| Acteur | Peut faire | Ne peut pas faire |
|---|---|---|
| Radar / scout | proposer des candidats | composer la hotlist finale |
| Agent Univers | sélectionner des slots d'attention | soumettre un ordre |
| Code d'activation | valider scope, quota, sticky et fallback | inventer une sélection agent |
| Brain trader | décider sur un symbole de l'univers actif | modifier l'univers hors du flux d'activation |

Le trader reçoit seulement le mandat projeté utile au symbole. Il applique
ensuite le même cycle de fraîcheur, contexte, gates et admission que les autres
décisions.

## 3. Comprendre le feedback D16 actif

D16 juge l'agent Univers comme allocateur d'attention : une sélection est
comparée au banc de candidats du même scope (`allocation`), et une vue
directionnelle peut aussi être jugée (`direction`). Le sync en arrière-plan
persiste les outcomes ; le digest agrégé, `agent` uniquement et soumis à un
seuil minimal, revient dans le prompt Univers.

Ce retour ne donne pas au digest le droit d'activer une hotlist, de modifier le
trader ou de court-circuiter le contrôle d'activation. La baseline de fallback
reste mesurée séparément.

## 4. Vérifier ce que tu lis

Pour les invariants et les formats actuels, consulte
[universe-trader-pipeline](../reference/universe-trader-pipeline.md),
[architecture de connaissance](../reference/agent-knowledge-architecture.md)
et le [registre D16](../decisions/registre-decisions-metier.md).
Les specs sous `docs/superpowers/` expliquent la conception au moment T : elles
ne remplacent pas ces références runtime.
