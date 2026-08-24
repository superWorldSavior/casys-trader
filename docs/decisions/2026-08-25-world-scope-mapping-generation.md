# Note de statut — Générations de World scope mapping (2026-08-25)

> **Type** : décision / statut (pas une réécriture du registre).
> **Lié à** : D19, pilote shadow 2026-08-24.
> **RFCs** : [macro source-only §6.2](../superpowers/specs/2026-08-23-world-model-macro-source-only-design.md).
> **Doc opérateur** : [opérer le World Model shadow](../how-to/operate-world-model-shadow.md).

## Décision

`world_scope_mapping.v1` est le **contrat de schéma**, pas une génération
de contenu. `universe.yaml` tourne ; le mapping n'est plus un hot-set
figé. Les symboles nouveaux sont réconciliés vers des scopes canoniques
typés (MIC, pays, région) depuis les métadonnées provider, en particulier
XNYS vs XNAS. Aucun fallback `US → XNYS`. Un échec provider laisse le
symbole `unresolved` / `unmapped`.

Le hash de contenu est la génération. Les lignes existantes sont
append-only (jamais réécrites). Un contenu nouveau crée un nouveau hash
et un nouvel identifiant d'instance d'ontologie
`market_ontology:v1:<mapping_sha256>`, sans `world_scope_mapping.v2` /
`market_ontology.v2`. Les cohortes déjà collectées restent pinées sur
leur hash. Une nouvelle cohorte pine le hash courant et la révision
dérivée. Les cohortes pilotes encore `COLLECTING` de la même forme
(même `mapping_id`, même signature de lanes, hash différent) sont
invalidées en append-only (`mapping_generation_drift`) avant d'armer B.
Elles restent lisibles. Les cohortes d'une autre forme ne sont pas
touchées.

Publication ontologique dans le **même** store append-only : store vide →
publier la génération courante. Store déjà publié, même famille de
schéma, nouvelle génération → append des têtes, publier la successeure,
superseder l'active. L'histoire PIT (`ready_at`) et les manifests déjà
collectés restent lisibles. Pas d'archive DB, pas de rewrite.

La réconciliation court au boot et après écriture d'univers (venue +
chemin legacy), fail-open pour Trader. CLI `world scope reconcile`
(dry-run par défaut, `--apply` pour persister). Les refs de génération
(mapping hash, révision ontologie, spec de pont) sont dérivées à
l'activation / composition ; `world_graph.yaml` et
`world_shadow_pilot.yaml` décrivent le schéma et la politique, pas le
hash de génération.
