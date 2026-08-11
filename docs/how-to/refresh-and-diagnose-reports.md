# How-to — Rafraîchir et diagnostiquer les rapports LLM

> **Type** : How-to (Diátaxis) — procédure opérateur.
> **Rapports** : global, macro, régional et micro · **Cockpit** : galerie
> Reports.

La touche `r` de la galerie recharge les fichiers présents sur disque ; elle ne
relance aucun agent. Commencer par identifier le type de rapport avant de
choisir une commande ou d'attendre le scheduler.

## 1. Identifier la projection concernée

| Rapport | Projection affichée | Déclenchement normal |
|---|---|---|
| Global | `state/global_universe_postures/current.json` | runner Univers, pré-open ou cooldown de 4 h |
| Macro | `state/news_briefs/latest-<VENUE>.jsonl` | régional : nouveau scope ou inputs changés après 4 h ; GLOBAL : inputs changés après 4 h ; `--force` pour les deux |
| Régional | `state/universe_runs/latest-<VENUE>.json` | runner Univers sur scope candidat pré-open |
| Micro | `state/company_intelligence/current/*.json` | changement matériel, pré-open ou cooldown de 24 h |

Les projections `latest` conservent le dernier succès utilisable. Une tentative
ultérieure en erreur apparaît sous `latest_failure` au lieu d'effacer le
rapport. La posture globale garde sa panne séparément dans
`state/global_universe_postures/latest_failure.json`.

## 2. Vérifier le daemon et les artefacts

```bash
uv run casys-trader status --json
```

Pour une venue, par exemple `TW` :

```bash
jq '.TW | {last_success_at,last_brief_ref,
     latest_failure:(.latest_failure | if . == null then null else
       {error_code,error_message,retry_attempt,next_retry_at} end)}' \
  state/news_macro_analysis_status.json

tail -1 state/news_briefs/latest-TW.jsonl | \
  jq '{brief_id,candidate_scope_id:.input_refs.candidate_scope_id}'

jq '{as_of,venue,status,candidate_scope_id,agent_run_id,
     agent_provider,agent_model,
     latest_failure:(.latest_failure | if . == null then null else
       {error_code,error_message,retry_attempt,next_retry_at} end)}' \
  state/universe_runs/latest-TW.json

jq '.venues.TW | {scope_phase,candidate_scope_id,
     last_universe_activation_scope_id}' state/venue_state.json
```

Comparer le `candidate_scope_id` du run régional avec celui du scope et du
brief macro. Un scope `close`, un brief absent ou un `brief_scope_mismatch`
n'est pas un rapport vide : la préparation exacte du pré-open n'est pas encore
disponible.

Pour les micros et leur file durable :

```bash
uv run casys-trader company-intelligence status
```

Le résultat sépare les compteurs `pending/running/done/dead`, le dernier succès
et la dernière panne. Un succès peut avoir `written=false` : l'appel a réussi
mais le brief équivalent existait déjà.

## 3. Rafraîchir un rapport macro

Une venue :

```bash
make macro MARKET=TW
```

Plusieurs venues, ou toutes les sorties macro :

```bash
make macro MARKET="TW EU US GLOBAL"
make macro ALL=1
```

Pour un diagnostic ponctuel après correction de la cause :

```bash
make macro MARKET=TW FORCE=1
```

`FORCE=1` ignore fraîcheur, cooldown et backoff de l'analyste macro. Ne pas le
boucler. Cette commande rafraîchit le **brief macro** ; elle ne force pas
directement un rapport régional. Si le daemon tourne et qu'un scope pré-open est
éligible, son prochain tick peut ensuite consommer ce brief.

La CLI s'exécute dans un autre processus que le daemon : leur protection
single-flight n'est pas partagée. Ne pas lancer cette commande pendant une
passe macro déjà visible dans les logs, afin d'éviter deux analyses concurrentes
et des écritures de statut concurrentes.

## 4. Rafraîchir un rapport micro

Un symbole, avec une attente bornée de la tâche :

```bash
uv run casys-trader company-intelligence refresh \
  --symbol 2330.TW --depth deep --wait
```

`--wait` attend au plus 600 s par défaut. Il peut donc rendre
`completed=false` avec une tâche encore `pending` : le premier retry n'est dû
qu'après 30 min. Utiliser `--wait-timeout-s` pour modifier cette attente, sans
confondre son expiration avec un échec terminal.

Le scope courant ou seulement l'univers actif :

```bash
uv run casys-trader company-intelligence refresh --scope current --wait
uv run casys-trader company-intelligence refresh \
  --scope active --depth deep --wait
```

Il n'existe pas de flag `--force` micro. Si les preuves sont inchangées et le
brief encore dans sa cadence, la réponse indique un `skipped` normal. Une panne
LLM est réessayée par la file jusqu'à six appels : 30 min, 1 h, 2 h, 4 h puis
6 h ; le sixième échec rend la tâche `dead`.

Lorsqu'un brief est écrit par le worker intégré au daemon, son callback réveille
seulement sa venue. Les écritures d'une même vague sont alors coalescées pendant
120 s avant une éventuelle nouvelle préparation régionale. La commande CLI
construit son propre runtime sans ce callback : son brief reste dans le store et
sera consommé lors du prochain passage Univers autrement éligible. La CLI seule
ne déclenche pas ce passage.

## 5. Laisser le daemon gouverner les rapports régional et global

Il n'existe pas de commande publique pour forcer directement ces deux agents.
Le rapport régional exige un scope final pré-open et un brief du même scope. La
posture globale est préparée par le même runner, selon sa fenêtre et son
cooldown.

Une activation agent réussie rend le `candidate_scope_id` exact terminal : un
nouveau micro arrivé après cette activation reste disponible pour le prochain
scope, mais ne recompose pas la hotlist active. C'est un skip attendu, pas une
panne.

Après l'envoi d'une requête valide à un agent, laisser le daemon atteindre
`next_retry_at` : macro, régional et posture globale suivent 30 min, 1 h, 2 h,
4 h puis un plafond de 6 h. Une erreur locale d'écriture de la projection
préparée utilise le backoff court de 1 à 30 min. En revanche,
`invalid_universe_request` et `candidate_scope_integrity_mismatch` signalent une
entrée invalide avant appel agent : elles n'ont ni backoff planifié ni
`next_retry_at`, et seront réévaluées au prochain tick. Corriger la cause au
lieu d'attendre. Redémarrer le daemon ne supprime aucune échéance persistée.

## 6. Lire la trace de retry

```bash
rg -e 'news macro retry (scheduled|deferred)' \
  -e 'universe retry (scheduled|deferred)' \
  -e 'global posture retry (scheduled|deferred)' \
  -e '\[queue.ledger\] (retry|dead) kind=company_micro' \
  state/daemon_console.log | tail -50
```

- `scheduled` : une panne vient de programmer la prochaine tentative ;
- `deferred` : un tick a vu la panne mais respecte encore le backoff ;
- `next_at` / `next_retry_at` : première échéance normale ;
- `latest_failure` avec une projection `status=success` : le dernier bon rapport
  reste affichable, mais la tentative la plus récente a échoué.

Après une réussite, revenir dans Reports et utiliser `r` pour relire les
projections.

## Voir aussi

- [Rapports micro](../reference/company-intelligence.md)
- [Macro/news](../reference/news.md)
- [Rotation et rapports régionaux](../reference/universe-rotation.md)
- [Lire les logs](read-logs.md)
