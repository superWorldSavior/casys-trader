# World model de marché — expérimentation shadow

> **Type** : Explanation (Diataxis). Cette capacité est observationnelle et
> `shadow_only` : elle ne conseille pas le Brain et ne pilote ni scheduler,
> RiskGate, broker, portefeuille, mandat Univers, ni mémoire.
> Contrat runtime : [référence World Model](../../reference/world-model.md).

## Owners

`WorldEpisode` est implémenté dans `trader.domain.world_episode` : preuve
marché action-free, jamais une mémoire Brain ou Univers. Le cas d'usage
déterministe (`WorldModelService`, capture, labeler, baseline Markov, GRU) vit
dans `application/world_model`. Le journal append-only et le query adapter
lecture seule vivent dans `infrastructure/state_db`. L'évaluation préquentielle
et l'impact Trader sont des read models
(`reporting/read_models/world_evaluation.py`, `world_impact.py`,
`world_status.py`). Le runtime n'ajoute que l'adapter background fail-open et
une façade de composition. Le CLI est un adaptateur mince : pas de SQL.

## Question à laquelle il répond

Le world model estime une transition exogène :

> « À partir d'un état de marché réellement disponible à T0, quelle est la
> probabilité que ce marché soit `DOWN`, `FLAT` ou `UP` à un horizon fixe ? »

Il ne répond pas directement à « ce trade était-il bon ? ». Cette seconde
question reste celle du critic Trader/FLAIR, qui peut comparer l'action choisie
à la trajectoire observée. D16 juge la sélection Univers et MemRL juge l'utilité
des rappels. Ces trois critics ne sont jamais fusionnés avec la cible marché.

## Flux V1

```text
snapshot marché frais, avant les gates et le LLM
  -> WorldEpisode immuable pour chaque symbole tradable avec ancre OHLCV valide
  -> prédictions baseline + GRU à 4 h et 1 j persistées avant leurs labels
  -> outcomes marché indépendants à T+4 h et T+1 j
  -> mise à jour incrémentale des deux modèles
  -> comparaison préquentielle appariée des prédictions shadow
```

Le sampling porte sur tout `tradable_symbols`, pas sur les seuls symboles dus,
admis par le scheduler ou envoyés au modèle. L'identité canonique combine la
place, le symbole, l'intervalle, la barre T0 et les versions de contrats de
features et de sampling. Un replay strict est un no-op ; une même identité avec
un contenu de marché différent est un conflit, jamais un overwrite. Deux wakes
sur la même barre réutilisent en revanche la première preuve canonique : une
heure de fetch ou un âge de fraîcheur plus tardifs ne créent pas un faux nouvel
état.

La V1 utilise les snapshots déjà chargés par les cycles Trader. Elle retire donc
le biais de sélection *entre symboles* dans un cycle, mais ne prétend pas encore
constituer une horloge de marché continue indépendante de la cadence des cycles.
La politique de sampling le dit explicitement afin qu'une politique périodique
future ne soit pas mélangée au même cohort.

## Observation et labels

L'observation est construite par whitelist à partir des seules barres et
métadonnées de marché disponibles à T0. Ce cutoff commun est pris après la fin
des I/O du snapshot, jamais à l'heure antérieure de début du cycle. Elle conserve notamment source,
intervalle, sémantique du timestamp, fraîcheur, `available_at`, barre d'ancrage,
features causales et hashes de preuve. Sans ancre OHLCV valide, **aucun
épisode** n'est émis.

Elle exclut action, intent, quantité, confiance, prompt, outils, tâche,
scheduler, portefeuille, risque, fills, PnL, et toute mémoire Brain/Univers.
Les mandats Univers et la décision Brain restent des contrôles séparés ; le
feature builder ne les lit pas.

Les horizons `elapsed_4h.v1` et `elapsed_1d.v1` sont calculés séparément depuis
`anchor_end_at` (heure de clôture de la barre d'ancrage), pas depuis un cutoff
de cycle. Chaque label utilise la première barre admissible à ou après sa
cible, conserve sa preuve et son retard réel, et peut rester
`pending`, ou se fermer en `observed`, `missing` ou `unknown`. Seul `pending`
est recalculé ; les états terminaux sont persistés pour éviter une rematuration
sans borne. Il n'existe aucun fallback silencieux de 1 j vers 4 h. Les classes
sont `DOWN` / `FLAT` / `UP` à bande 50 bp. Le store
accepte une correction future comme outcome supersédant ; un
producer de correction devra l'ajouter explicitement et ne modifiera jamais
une preuve existante. Le service fingerprint les feuilles actives et reconstruit
les deux modèles dans un ordre canonique dès que ce ledger change : une arrivée
retardée ou une correction produit donc le même état live qu'après redémarrage.

## Baseline et GRU challenger

Le baseline est un Markov catégoriel hiérarchique à lissage
Dirichlet : état exact, puis état grossier, puis fréquence globale, puis
distribution uniforme au cold start. Il est déterministe, incrémental et rend
visibles son support et son niveau de backoff. Tant que le support est
insuffisant il émet `warming_up/NO_GO`; avec davantage de données il reste
malgré tout `shadow_only`.

Le challenger est un petit GRU NumPy qui consomme les douze derniers épisodes
compatibles d'un même instrument. L'encodeur partagé vit dans `encoding.py` :
le GRU n'importe pas le baseline. Aucune normalisation ni aucun vocabulaire
n'est ajusté sur le futur. Chaque horizon possède ses propres poids et reçoit
une étape BPTT déterministe seulement lorsque son label append-only devient
disponible. Le démarrage est donc volontairement froid, avec padding masqué,
puis l'apprentissage se fait progressivement à partir du premier label
prospectif ; il n'existe pas de gros entraînement initial caché.

Le baseline et le GRU produisent tous les deux le même `WorldPrediction`, pour
la même observation et avant son outcome. Ils restent en permanence
`shadow_only/NO_GO`. Un redémarrage reconstruit leur état en rejouant les
épisodes puis les labels dans leur ordre causal ; le GRU n'est ni une nouvelle
mémoire du Brain ni une source d'autorité de trading.

## Mesures shadow et apport Trader

L'évaluation (owner reporting) groupe chaque version de modèle et chaque
horizon. Elle mesure Brier, log-loss, accuracy, calibration à cinq bins,
direction et un drawdown directionnel de marché. La comparaison GRU–baseline
est appariée sur les mêmes épisodes : en dessous du support minimum **20**
elle répond `insufficient_support` au lieu de déclarer un gagnant. Le drawdown
de marché est un proxy additif à notionnel unitaire, sans frais, FX ou sizing ;
les horizons peuvent se chevaucher. Ce n'est donc pas un drawdown de
portefeuille. Les colonnes indexées `model_kind` et `move_class` priment sur
un claim de payload.

L'apport aux résultats Trader est une autre question. Une prédiction World
asynchrone n'est actuellement ni présentée au Brain ni référencée par la
décision. Le read model d'impact retourne honnêtement
`actual_contribution=not_attributable` et
`counterfactual_contribution=not_available`. Il ne transforme jamais une bonne
prévision de marché en faux gain Trader.

Une future association descriptive exigera un lien append-only explicite entre
la prédiction et la décision, `ready_at` (disponibilité réelle de persistance),
l'heure de dispatch de la décision, puis un cycle flat-to-flat à frais connus.
Un vrai delta PnL/drawdown exigera en plus une politique shadow
pré-enregistrée et son ledger d'ordres/fills simulés. `predicted_at` (cutoff
logique) et `cycle_ts` (début de cycle) ne suffisent pas à prouver l'ordre
temporel réel.

## Persistance et sûreté

`state/world_model.db` est le journal canonique dédié de cette preuve live. Il
n'est ni une table de `casys.db`, ni une projection de `decisions.jsonl`.
Episodes, outcomes et prédictions sont append-only ; SQLite WAL et des
transactions courtes assurent l'idempotence, et les `UPDATE/DELETE` sont
refusés. Si le daemon n'a pas encore créé le fichier, le live répond
`not_started`.

Le hook runtime est fail-open et isolé par symbole/horizon. Toute panne du
store, du labeler ou du modèle est rapportée dans le résultat shadow, sans
modifier ni faire échouer le cycle métier. Les décisions historiques n'ayant
pas de snapshot T0 exact peuvent servir à l'audit de jointure, mais restent
`training_eligible=false` et ne sont pas injectées dans ce modèle.

Le shadow est actif par défaut (`CASYS_WORLD_MODEL_SHADOW_ENABLED=1` au boot).
`=0` le désactive sans toucher au chemin de décision. Le flag n'est lu qu'au
démarrage : un changement n'a d'effet qu'après redémarrage du daemon.

Le statut est une lecture seule : il ne crée ni ne migre la base absente.

```bash
uv run casys-trader world status --json
```

Il expose la couverture, les prédictions par modèle et, dès que des prédictions
ont mûri, les groupes préquentiels, la comparaison appariée baseline–GRU et les
mesures d'impact avec leurs limites d'attribution. Fichier absent →
`not_started`. Base créée mais rien de scorable → `warming_up`.
