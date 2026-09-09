# Référence — Dérivation World Macro

> **Type** : Reference (Diátaxis).
> **Code** : `trader/domain/world_macro.py`,
> `trader/application/world_model/macro_pipeline.py`,
> `trader/infrastructure/market_sources/world_macro/`,
> `trader/runtime/world_macro_runtime.py`,
> `trader/reporting/read_models/world_macro_status.py`
> **Config** : `config/world_macro_derivation_policy.yaml`,
> `config/world_macro_sources.yaml`

La dérivation des régimes macro est source-only, déterministe, et refuse
d'inventer une valeur. Les adaptateurs émettent des `MacroNumericValue`.
Le projecteur exécute les règles hachées de la politique ; il ne vote pas
des catégories que les sources n'ont jamais produites.

## Horloges

DBnomics n'atteste pas la date de publication d'une observation. Distinguer :

| Horloge | Origine | Rôle |
|---|---|---|
| `period` / `occurred_at` | période économique de la série | vintage du fait ; ancre conservative du TTL |
| `indexed_at` | horloge d'indexation de la série DBnomics | audit fournisseur uniquement ; **jamais** `published_at` ni TTL |
| publication statistique | absente du JSON/CSV DBnomics | non inventée |
| `ingested_at` | horloge locale d'acquisition | reconstruction ; n'entre pas dans `valid_until` |
| `receipt.ready_at` | première disponibilité persistée | éligibilité PIT ; interdit le lookahead |
| `valid_until` | `derive_macro_source_fact_valid_until` (`occurred_at` + TTL) | fraîcheur conservative, rejouable ; `policy_until_superseded` seulement si `source_id` ∈ `until_superseded_source_ids` |

Interdit : remplacer les horloges par `now`, prendre `indexed_at` pour une
publication, ou relabeliser un CPI / chômage périmé en frais. Un refetch du
même couple période/valeur ne prolonge pas `valid_until`.

Yahoo (`market_benchmark`) garde la date de séance comme vintage : c'est la
date de l'observation de marché, pas une publication statistique.

Un taux directeur **en vigueur jusqu'au prochain changement** n'est pas un
taux effectif quotidien. `ecb_deposit_rate` lit
`ECB/FM/D.U2.EUR.4F.KR.DFR.LEV` (niveau DFR, remplissage calendaire quotidien
de la même série « date of changes ») et applique
`policy_rate_until_superseded_d` (56 j) depuis le vintage de période, pas
depuis `now`. `fed_funds_effective` reste `FED/H15/RIFSPFF_N.D` (overnight
effectif) avec le TTL quotidien 72 h : ce n'est pas le target FOMC.

## Politique `macro_regimes.v1`

Le YAML est haché (`content_sha256`). Changer un seuil change le hash. Le
`transform_version` nomme cet ensemble de règles.

| Dimension | Méthode | Résultat connu seulement si |
|---|---|---|
| `rates_regime` | `last_two_admissible_points_delta_pp` | deux périodes distinctes, même scope / même série / même unité, toutes deux admissibles au cutoff |
| `usd_regime` | `always_unknown` | jamais : `broad_usd_index` est un gap déclaré, aucun feed USD n'est approuvé |
| `macro_regime` | `always_unknown_until_inflation_yoy_proven` | jamais tant qu'un YoY n'est pas une preuve validée ; un niveau d'indice CPI ne fabrique pas de YoY ; Brent/or ne votent pas |

Seuils de taux (points de pourcentage) :

- `rising` si `Δ ≥ 0.125`
- `falling` si `Δ ≤ -0.125`
- `stable` si `\|Δ\| < 0.125`
- un seul point, conflit multi-séries, unités distinctes → `unknown`

Les faits numériques de plusieurs fournisseurs peuvent coexister dans le
store. Ils ne votent pas ensemble. Un scope US n'utilise pas la BCE ; un
scope Europe n'utilise pas la Fed.

La couverture **source** (`MacroCoverage`) n'est pas la couverture
**dimension**. Une collecte US complète sur les sources n'implique pas un
`rates_regime` connu.

## Acquisition

DBnomics v22 traite `observations` comme un booléen (`1` / `true` = inclure
les observations, en pratique toute la série). Ce n'est pas un compte.
`observations=2` désactive les observations. L'adaptateur demande
`observations=1&metadata=0`, parse les tableaux `period` / `value`, et
n'émet que les **deux dernières** périodes finies. Le cache 24 h rejoue ce
tuple ; il ne renouvelle pas un vintage périmé.

Provenance primaire vérifiée (GET borné, 2026-09-08, sans secret) :

- JSON `https://api.db.nomics.world/v22/series/FED/H15/RIFSPFF_N.D?observations=1&metadata=0` : 26 363 points, dernier `2026-09-03 = 3.63`, `indexed_at` série `2026-07-25T01:26:07.820Z`. Au 8 septembre le TTL 72 h depuis le vintage `2026-09-03` est déjà échu (dernier jour ouvré Fed avant week-end + Labor Day).
- JSON `https://api.db.nomics.world/v22/series/ECB/FM/D.U2.EUR.4F.KR.DFR.LEV?observations=1&metadata=0` : 10 110 points, dernier `2026-09-05 = 2.25`. Même définition que `B.U2` (niveau DFR) mais remplissage quotidien ; `B.U2` n'a plus d'observation après `2026-06-17`.
- IMF CPI US (`IMF/CPI/M.US.PCPI_IX`) s'arrête en `2025-07` ; BLS chômage (`BLS/ln/LNS14000000`) en `2025-01` ; HICP EA20 (`Eurostat/prc_hicp_midx/M.I15.CP00.EA20`) et HICP EA changing composition (`M.I15.CP00.EA`) en `2025-12`. OECD/MEI CPI US plus ancien (`2023-12`). Pas d'équivalent DBnomics courant pour ces dimensions.

Les périodes CPI / chômage / HICP périmées restent périmées après refetch. USD et `macro_regime` restent `unknown`.

## Lookahead

Un point appris après le cutoff (receipt `ready_at` postérieur) n'est pas
admissible à ce cutoff. Reconstruire un as-of passé à partir de faits
découverts plus tard est interdit. Les observations déjà persistées ne sont
pas réécrites.

## Statut

`read_world_macro_status` projette collecte, couverture source, couverture
par dimension, et fraîcheur. `status.json` n'est pas une autorité PIT.
Autorité inchangée : `shadow_only` / `decision_effect=none` / `NO_GO`.

## Adoption opérationnelle

Le correctif s'applique aux **nouvelles** collectes. Les faits déjà persistés
avec `published_at = indexed_at` gardent leur identité et leur TTL erroné ;
ils ne sont pas mutés. Un daemon déjà lancé continue l'ancien adaptateur
jusqu'au prochain déploiement. Aucun restart n'est fait par ce lot.
