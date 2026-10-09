# World dynamics : apprentissage OHLCV automatique en shadow

Le World Model existant prédit trois classes à des horizons fixes. Ce prototype
ajoute un apprentissage de **transitions d'état OHLCV** et une simulation
récursive de plusieurs barres. Il reste un modèle de marché exogène, par
instrument, sans action de trading. Il ne représente pas encore les facteurs
communs marché/secteur, les événements futurs ou un état latent multimodal.

## Réemploi et frontières

- `WorldEpisode` et `AnchorBar` sont réutilisés pour le rejeu du ledger. La
  collecte automatique utilise `ObservedDynamicsBar`, une preuve distincte
  de barre réelle avec horloges de première réception et de persistance ;
  elle ne fabrique pas d'épisode passé. Les
  cibles proviennent de deux ancres réellement adjacentes, avec mêmes
  instrument, intervalle, source, sémantique et contrats. Un trou ou une nuit
  ne deviennent jamais artificiellement « la prochaine barre ».
- Le noyau NumPy GRU est partagé avec le classifieur existant. La tête de
  régression appartient au nouveau modèle ; la tête trois classes, ses
  identités et ses snapshots restent dans le classifieur. Un golden capturé
  avant extraction vérifie les octets des paramètres, fingerprints et
  probabilités des horizons froid et appris.
  Les fichiers partagés appartiennent au périmètre de `lane_code_hash` :
  cette extraction change l'empreinte du code lors d'une nouvelle attestation,
  même si les sorties sont identiques. Les gardes de compatibilité des cohortes
  restent applicables ; aucun manifeste n'est réécrit pour les contourner.
- Le domaine possède les invariants des transitions, barres simulées et
  trajectoires. L'application possède modèle, port de modèle et rejeu.
  L'infrastructure lit un scope SQLite borné en lecture seule, ou conserve
  un journal OHLCV borné avec écriture atomique synchronisée. Le reporting
  compose et projette ; le CLI ne contient pas de SQL.
- Aucun schéma de ledger, receipt, manifeste de cohorte ou chemin de décision
  live n'est ajouté. Les modèles simulés n'alimentent jamais l'apprentissage.
  L'autorité reste `shadow_only`, l'effet `none`, la recommandation `NO_GO`.

## Modèle et incertitude

La tête GRU apprend cinq coordonnées : logarithme du gap d'ouverture, du corps,
de la mèche supérieure, de la mèche inférieure, et variation du logarithme
`log1p(volume)`. Leurs échelles fixes sont versionnées ; aucune normalisation
n'est estimée sur le futur du jeu de test. La séquence est bornée à quatre
états et son padding est masqué.

La distribution utilise des **vecteurs résiduels complets**, calculés avant
chaque mise à jour sur des cibles déjà disponibles. Le bootstrap conserve les
associations entre coordonnées et sa fenêtre est bornée à 256 transitions.
Les dépendances temporelles du bruit et sa calibration ne sont pas établies.
Le baseline échantillonne les vecteurs de transitions réelles passées avec le
même décodeur, sans réseau récurrent.

Le décodeur garantit des prix positifs et l'ordre OHLC. Les mèches négatives
échantillonnées et un `log1p(volume)` négatif sont rectifiés à zéro selon une
politique explicite versionnée. Une valeur qui déborde la représentation
numérique provoque une erreur ; les prix ne sont pas plafonnés silencieusement.
Chaque barre simulée devient l'entrée de l'étape suivante. Chaque trajectoire
conserve sa propre seed PCG64, l'origine réelle, le fingerprint et le cutoff
d'apprentissage. Le contexte consommé est explicitement `market_only`.

## Rejeu et évaluation

La disponibilité effective vaut
`max(available_at, captured_at, recorded_at)`. À chaque origine, seules les
cibles déjà disponibles entraînent les modèles ; aucune horloge passée n'est
reconstruite. Le premier OHLCV connu d'un slot est fixé pour l'expérience.
Les variantes identiques ne multiplient pas les exemples. Un conflit dès la
première horloge exclut le slot ; une correction ultérieure est signalée et
ne réécrit jamais l'apprentissage passé. Les cibles évaluées sont donc les
premières barres connues, pas un historique corrigé rétrospectivement.

Les derniers 30 % des origines constituent la fenêtre de test. Au plus 64
origines sont choisies régulièrement dans le temps avant inspection de leurs
labels. L'apprentissage continue dans cette fenêtre uniquement après
disponibilité des cibles, selon un protocole préquentiel. Une origine avec
futur incomplet, déjà connu ou déjà terminé à l'heure de prédiction n'est pas
scorée. Les mêmes origines et horizons comparent GRU et baseline : CRPS des
rendements, erreur absolue de la moyenne, couverture et largeur de l'intervalle
central à 80 %. Ces origines peuvent se chevaucher et restent corrélées.

C'est une exploration historique, sans preuve prospective de supériorité,
sans claim causal ou PnL. La grille simulée est à intervalle fixe et ne valide
pas le calendrier d'ouverture de la bourse. Une origine dont la première
barre suivante est déjà terminée au cutoff reçoit `stale_origin` sans rollout.

## Activation et suivi automatiques

Le daemon compose cette extension **par défaut** quand son World Model shadow
est activé (`CASYS_WORLD_MODEL_SHADOW_ENABLED`, défaut `1`). Aucun lancement
manuel de rejeu n'est nécessaire. Le même worker de fond reçoit les historiques
OHLCV déjà récupérés par le cycle marché, sans nouvelle requête réseau ni appel
Jev. `CASYS_WORLD_DYNAMICS_SHADOW_ENABLED=0` permet une désactivation explicite.
Le code et cette variable sont lus au démarrage du daemon.

```sh
uv run casys-trader world dynamics status
uv run casys-trader world dynamics status --json
uv run casys-trader world status --json
uv run casys-trader status --json
```

Le cockpit affiche également la ligne World Dynamics. Le suivi de Dynamics
lit un petit statut enregistré, sans réentraînement ni lecture des historiques
du journal. Le PID vivant et son appartenance au daemon sont vérifiés
séparément : `worker_stopped` conserve le dernier rapport, sans annoncer un
ancien résultat comme une activité actuelle.

Le journal `state/world_dynamics/bars.json` conserve au plus **quatre séries
épinglées et 256 barres par série**. Elles sont choisies au premier snapshot
éligible, puis retrouvées au redémarrage. Les séries supplémentaires, trous,
barres incomplètes et révisions exclues sont comptés. Le modèle est reconstruit
déterministement sur cette fenêtre lorsque les preuves retenues changent ;
un snapshot identique réutilise le résultat. La reprise conserve les premières
horloges de réception, sans les réinitialiser. Aucune trajectoire synthétique
ne devient une preuve d'apprentissage.

La collecte initiale peut apprendre l'historique **maintenant**, mais cet
historique n'est jamais scoré comme une prévision faite autrefois. La
disponibilité vaut `max(first_seen_at, recorded_at)` pour ces preuves. Le
worker génère 20 trajectoires de quatre étapes après 40 transitions adjacentes,
avec au plus huit origines d'évaluation. Une origine devenue périmée perd ses
trajectoires, même si aucun nouvel historique n'est arrivé.

Le statut décrit `waiting_for_data`, `warming_up`, `ready`, `stale_origin`,
`paused`, `disabled` ou une erreur explicite, avec les supports et raisons par
série. Les rapports détaillés ont un chemin stable dans
`state/world_dynamics/reports/`, indiqué par `series[].report_path`. Le statut
global est `state/world_dynamics_status.json`. Le garde-fou de ressources du
worker existant s'applique aussi à cette maintenance ; ses erreurs restent
isolées du cycle de trading.

## Rejeu manuel du ledger

Depuis le checkout voulu, avec son propre `state/world_model.db` :

```sh
uv run casys-trader world dynamics \
  --venue TW --symbol 8046.TW --interval 15m \
  --start 2026-09-20T00:00:00Z --as-of 2026-10-09T12:00:00Z \
  --steps 4 --paths 200 --min-support 40 --seed 0 --json
```

L'intervalle est celui des ancres, pas l'horizon du classifieur. Quatre étapes
de 15 minutes représentent une heure sur la grille fixe. Le scope fixe aussi
les versions des contrats marché et sampling ; leurs options CLI permettent
de choisir explicitement le contrat existant approprié. Le lecteur refuse
les scopes mélangeant les feeds. Il refuse une fenêtre dépassant `--limit`
plutôt que de tronquer l'historique. Une DB absente n'est jamais créée.

Le rapport donne les exclusions, paramètres, preuves des origines appariées,
quantiles de toutes les trajectoires et trois trajectoires complètes à titre
d'exemples. `no_data`, `insufficient_support` et `stale_origin` sont des
résultats descriptifs ; une corruption ou un dépassement de borne est une
erreur explicite.

## Historique du ledger et place de Jev

Le ledger capture une ancre par passage, pas la bande OHLCV complète. Sur la
lecture du 9 octobre pour `TW/8046.TW/15m`, du 20 septembre au cutoff du
9 octobre à 12 h UTC, sept ancres éligibles donnaient **zéro transition
adjacente**, donc `insufficient_support`. Ce constat concerne ce scope précis.
Le nouveau journal automatique conserve désormais les historiques en mémoire
avec leur première disponibilité réellement observée. Il ne transforme pas un
téléchargement actuel en preuve connue dans le passé et n'étend pas le ledger
des épisodes pour y injecter ces barres historiques.

Jev a aussi été examiné à partir d'un benchmark local séparé du 20 septembre
2026, hors du périmètre de ce prototype. Il mesurait une prévision directe :
299 réponses valides, toutes FLAT, accuracy 31,77 % et Brier 1,1559 sur le lot
principal. Ces résultats n'établissent pas de valeur prédictive. Le prototype
fonctionne sans client TypeSafe et sans nouvelle dépendance.

Les primitives officielles sont des probabilités catégorielles ou binaires,
et des niveaux descriptifs ordonnés. Elles ne fournissent pas une distribution
jointe OHLCV continue : cinq questions indépendantes ne constituent pas une
trajectoire cohérente. Jev peut devenir un challenger sur les mêmes origines,
informations et labels hors échantillon, ou noter un ensemble de scénarios
préconstruits, après validation dédiée. Ses prédictions resteraient séparées
des observations réelles. Aucun appel payant n'est nécessaire à ce prototype.

Sources vérifiées : [primitives TypeSafe](https://docs.typesafe.ai/primitives),
[Choice](https://docs.typesafe.ai/primitives/choice),
[Score](https://docs.typesafe.ai/primitives/score),
[modèles](https://docs.typesafe.ai/models).

## Vérifications du 9 octobre

Au premier commit du prototype, avant l'activation automatique :

- 129 tests ciblés passent dans le checkout principal : domaine, modèles,
  causalité du rejeu, lecteur, CLI réel et frontières DDD.
- Ruff passe sur tous les chemins modifiés. Le lint global rencontre cinq
  imports inutilisés déjà présents dans `test_world_pattern_scoring_e2e.py`.
- La suite globale reste rouge. Les 73 échecs hors des tests nouveaux ont été
  reproduits sur le snapshot initial, sans l'extension. Les tests nouveaux
  ont été relancés dans un processus neuf après stabilisation des fichiers.
- Une lecture seule du ledger réel et un appel au CLI installé confirment
  le résultat `insufficient_support` du scope décrit ci-dessus.

Pour l'activation automatique, **303 tests ciblés passent sur le contenu exact
du commit**, ainsi que Ruff sur les chemins concernés. Ils couvrent aussi la
composition réelle du worker : bootstrap, réception dupliquée, reprise
déterministe, panne de capture indépendante, journal corrompu, pause de
ressources et désactivation. Aucun appel payant Jev ni nouveau paquet. Le
daemon courant est vérifié séparément de l'intégration du code.
