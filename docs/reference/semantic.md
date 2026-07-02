# Référence — Couche sémantique (catalogue d'indicateurs)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/semantic/catalog` · **Rôle** : catalogue **gouverné** des indicateurs de trading.

Une couche sémantique au-dessus des indicateurs bruts : elle nomme, décrit,
regroupe et rend **recherchables** les indicateurs, et traduit les labels
catégoriels en valeurs. Utilisée par les outils domaine (`find_indicators`,
`describe_data`) et le vocabulaire des veilles.

## Ce qu'elle expose

| Fonction | Rôle |
|---|---|
| `IndicatorSpec` | spec d'un indicateur : `name`, `label`, `description`, `category`, `inputs`, `output`, `concepts` (les timeframes vivent dans le dict module `TIMEFRAMES`) |
| `list_indicators()` | liste tous les indicateurs du catalogue |
| `find_indicators(concept)` | recherche par **concept** (ex. « momentum » → indicateurs pertinents) |
| `family_for_symbol(symbol)` | famille thématique d'un symbole |
| `label_to_value(indicator, label)` | traduit un **label catégoriel** en valeur numérique (ex. veilles à conditions labellisées) |
| `normalize_temporal_query(...)` | normalise une requête temporelle (timeframes, fenêtres) |

## Où c'est branché

- **Domain tools** : `find_indicators`, `describe_data` (cf. [agent-tools](agent-tools.md)).
- **Veilles** : `label_to_value` permet des conditions d'indicateur labellisées
  (normalisées atomiquement — 1 condition rejetée → 0 veille).
- **Contexte** : les timeframes/familles alimentent le [contexte agent](agent-context.md).

Le catalogue est **gouverné** : source unique de vérité pour les noms/familles
d'indicateurs (évite les alias ambigus — principe AX « no verb overlap »).

## Voir aussi
- [Domain tools](agent-tools.md) · [Contexte agent](agent-context.md) · [Contrat LLM](llm-contract.md) (vocabulaire veilles).
