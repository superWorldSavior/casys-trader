# Plan — exécution native Grok des lots World Model

## Statut

Workflow préparé et smoke-checké ; aucun lot d'implémentation n'a été lancé.
Le runtime Trader, le daemon, `state/**` et `desktop/**` restent inchangés par ce
plan.

## Autorité de conception

Le workflow exécute exactement un lot défini par l'une de ces RFCs :

- [flux macro source-only](../specs/2026-08-23-world-model-macro-source-only-design.md) ;
- [cohorte prospective appariée](../specs/2026-08-23-world-model-prospective-cohort-design.md) ;
- [graphe temporel et hypothèses de patterns](../specs/2026-08-23-world-model-graph-pattern-hypotheses-design.md).

Les objets de domaine, événements, ports, cycles de vie, `allowed_edits`, tests
et `depends_on` des RFCs priment sur le workflow. Les 29 lots exécutables sont
`MACRO-0..7`, `COHORT-0..7`, `GRAPH-1..12` et `GRAPH-3B` ;
`MACRO-CONFIG` et `GRAPH-CONFIG` sont des gates opérateur, jamais des lots.

## Workflow local

Le fichier est `.grok/workflows/world-model-lot.rhai`. `.grok/` est ignoré par
la convention Git actuelle du dépôt : la définition reste donc locale, tandis
que ce plan versionné fixe son contrat d'usage. Son SHA-256 validé est :

```text
302ad93c46e5d4aa9a3e5b409d1cee58cab3575c7fca00e74f3b84d4066c5427
```

Le workflow Grok natif suffit ici. ACPX n'est pas retenu : il n'apporterait de
valeur que pour une orchestration cross-provider, une queue externe ou un DAG
CI persistant absent de ce chantier.

## Entrées et invocation

Une exécution reçoit exactement :

```json
{
  "lot_id": "COHORT-0",
  "rfc_path": "docs/superpowers/specs/2026-08-23-world-model-prospective-cohort-design.md",
  "base_commit": "<sha Git vérifié>"
}
```

Depuis Grok, lancer `/world-model-lot` avec cet objet, ou
`/workflow world-model-lot ...`. Le budget requis est de 12 agents logiques ;
le fan-out maximal est 3. Un chemin sans correction consomme 8 appels et le
chemin complet avec fix puis re-vérification en consomme 12.

## Phases et garanties

1. **Preflight** — trois panels read-only vérifient dépendances réellement
   livrées, collisions du worktree et faisabilité des tests. Toute preuve
   absente donne `NO_GO` sans édition.
2. **Implement** — un seul writer, TDD, uniquement dans les `allowed_edits` du
   lot, sans sous-agent ni anticipation du lot suivant.
3. **Verify** — trois panels read-only tentent de réfuter conformité RFC, DDD
   et preuves de tests. Le scope des chemins est contrôlé séparément par le
   script sur le diff Git et par un inventaire structuré.
4. **Fix** — au plus une reprise du même writer, uniquement sur constats
   confirmés et dans le même scope.
5. **Re-verify** — après un fix seulement, trois nouveaux reviewers relisent le
   worktree courant ; `addressed[]` déclaré par le writer ne vaut pas preuve.
6. **Integrate** — exécuteur sans édition : tests ciblés du lot, puis exactement
   `uv run pytest -q tests/package_layout`, puis les contrôles de diff.
7. **Complete** — verdict structuré ; l'autorité reste `shadow_only`, la
   recommandation Trader reste `NO_GO` et `decision_effect=none`.

Le script interdit commit, push, `git add -A`, restart/kill daemon, `.env`,
`state/**`, `desktop/**`, `docs/README.md` et le fichier de référence World
Model actuellement détenu par un autre scope. Le commit éventuel reste une
action séparée de l'orchestrateur, avec pathspecs explicites.

## Gates opérateur

Avant `MACRO-4`, ces trois artefacts doivent être commités, versionnés et
hashés :

- `config/world_macro_sources.yaml` ;
- `config/world_macro_derivation_policy.yaml` ;
- `config/world_scope_mapping.yaml`.

Avant `GRAPH-5`, `config/world_graph_v3.yaml` doit référencer l'ID/hash du même
`config/world_scope_mapping.yaml`, sans créer une seconde autorité de scopes.
Avant `GRAPH-7`, il doit aussi geler `cohort_id` et la règle de
fermeture/multiplicité. Grok valide ces choix mais ne les invente pas.

## Ordre de livraison

Après `MACRO-0`, le pilote non-macro C1 (`COHORT-0..7`) peut avancer en parallèle
des lots source-only `MACRO-1..4` et des fondations graphe dont les dépendances
sont satisfaites. Les collisions partagées sont ensuite sérialisées dans le bon
sens : `MACRO-5` après `COHORT-4`, `MACRO-6` après `COHORT-5`, `MACRO-7` après
`COHORT-7`. Le bridge prospectif `GRAPH-3B` attend le producteur macro et ne
backfille rien avant son watermark d'activation.

Protocole par lot : partir d'un `base_commit` explicite, laisser le preflight
vérifier les preuves livrées, exécuter un seul lot, relire le résultat et le
diff, puis faire séparément un commit borné avant de passer au suivant.

## Portée du smoke-check

Les chemins `validate_only` compilent le script entier et ont exercé des
preflights représentatifs, qui échouent fermés avec le host factice. Ils ne
prouvent ni les branches `Implement/Verify/Fix/Integrate`, ni les dépendances
live, ni les valeurs des YAML opérateur, ni l'exactly-once d'effets externes en
cas d'interruption. Le host expose directement les diffs trackés ; l'inventaire
staged/untracked dépend d'un agent execute-only et toute sortie absente produit
`NO_GO`. Un premier run réel devra donc commencer par un petit lot de fondation
sur un scope propre, jamais par l'activation runtime.
