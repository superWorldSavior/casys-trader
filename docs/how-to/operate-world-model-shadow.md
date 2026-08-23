# How-to — Opérer le World Model shadow

> **Type** : How-to (Diátaxis). Procédure lecture seule.
> **Référence** : [`reference/world-model.md`](../reference/world-model.md).
> **Ne pas** redémarrer, pousser, ni modifier l'état live depuis cette page
> sans intention explicite.

Le World Model est `shadow_only` / `NO_GO`. Il n'influence ni le Brain, ni
l'Univers, ni le broker. Une métrique de marché n'est **pas** un uplift Trader.

## Lire le statut

Depuis la racine du dépôt, sur l'état paper courant :

```bash
uv run casys-trader world status --json
```

Sortie machine-readable (`schema_version=world_model_status.v1`). Champs à
lire en premier :

| Champ | Lecture honnête |
|---|---|
| `status` | `not_started` si `state/world_model.db` n'existe pas encore |
| `authority` | toujours `shadow_only` |
| `decision_effect` | toujours `none` |
| `evaluation.status` | `warming_up` tant qu'aucune paire n'est scorable |
| `impact.actual_contribution.status` | `not_attributable` |
| `impact.counterfactual_contribution.status` | `not_available` |

La commande n'écrit rien : base absente → `not_started`, fichier intact.

## Interpréter `not_started`

C'est l'état live attendu si le daemon n'a jamais booté le shadow (flag off,
échec de boot, ou instance jamais démarrée avec ce code). Ce n'est pas une
preuve que le modèle « ne prédit pas ». Vérifier ensuite :

1. `CASYS_WORLD_MODEL_SHADOW_ENABLED` dans le `.env` du process **déjà lancé**
   (un export dans le shell courant ne change pas un daemon vivant) ;
2. présence de `state/world_model.db` ;
3. logs `[world_model_shadow]` dans `state/daemon_console.log`.

Le flag n'est lu qu'au démarrage. Pour l'activer ou le couper : arrêter le
daemon proprement, puis le relancer. Voir
[`run-the-daemon.md`](run-the-daemon.md).

## Activer le challenger contexte V2 (opt-in, défaut off)

`CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED` vaut `0` par défaut. Ce how-to ne
redémarre pas le daemon live et n'écrit pas dans `state/`.

1. Vérifier que le process **déjà lancé** n'a pas le flag (un export local
   ne le change pas).
2. Poser `CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED=1` dans l'environnement du
   prochain boot.
3. Seulement lors d'un arrêt/relance **volontaire** ultérieur : le boot
   construit le reader local et l'enricher background. Le cycle trading
   continue de n'enqueue que la V1 marché.
4. Relire `casys-trader world status --json` : `authority` reste
   `shadow_only`. Une ablation V2−V1 n'est pas un PnL Trader, pas un
   claim causal, et pas un impact de graphe : NetworkX est une projection
   de provenance/traversée ; la topologie n'est pas encore encodée dans
   le baseline/GRU.

Le code V2 doit démarrer une **cohorte prospective propre**. Interpréter
une ablation comme prête est `NO_GO` tant que les gates causales
(cutoff, reçus, ablation appariée) ne passent pas **et** qu'un producteur
macro *source-only* n'existe pas. Les briefs macro actuels sont contaminés
politique/candidat et restent exclus : un rapport régénéré demain recevra
un reçu d'availability valide mais restera exclu jusqu'à ce producteur.
L'incrément mesurable aujourd'hui est uniquement les métadonnées company /
status compactes causalement prouvées plus la missingness et la topologie
familiale déjà partagées avec la V1.

Détail d'ontologie : [`world-context-ontology.md`](../explanation/architecture/world-context-ontology.md).

## Activer / couper

| Objectif | Action |
|---|---|
| Shadow on (défaut) | omettre le flag, ou `CASYS_WORLD_MODEL_SHADOW_ENABLED=1`, **puis redémarrer** |
| Shadow off | `CASYS_WORLD_MODEL_SHADOW_ENABLED=0`, **puis redémarrer** |

Le chemin de décision Trader ne change pas. Couper le shadow n'efface pas
`world_model.db`.

## Lire l'évaluation sans sur-interpréter

Quand `evaluation.status=ready` :

- comparer baseline et GRU seulement si `comparisons[].status=ready` ;
- en dessous de 20 paires : `insufficient_support`, pas de vainqueur ;
- le drawdown directionnel est un proxy de marché à notionnel 1, sans frais ;
  ce n'est pas le drawdown du portefeuille paper.

Quand `impact.actual_contribution.status=not_attributable` : ne pas attribuer
de PnL Trader au World Model. Un lien pré-décision append-only et une
politique d'ordres shadow pré-enregistrée n'existent pas encore.

## Isoler la preuve

`state/world_model.db` n'est pas `casys.db`. Ne pas y copier de tables broker,
ni y ouvrir d'écriture manuelle. Le query adapter et le CLI sont en lecture
seule ; le store runtime refuse `UPDATE`/`DELETE`.
