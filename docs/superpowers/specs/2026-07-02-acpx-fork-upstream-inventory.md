# Inventaire — couche acpx maison, candidats à remonter dans un fork

**Date** : 2026-07-02
**Statut** : inventaire de cadrage (chantier « fork acpx » envisagé : repo public
GitHub, y intégrer ce que casys-trader compense aujourd'hui côté client, après
le rejet des PRs upstream).
**Source** : audit du code par agent (trader/llm.py, codex_client.py,
consolidator.py, process_env.py, cockpit_supervisor.py, docs/architecture.md).

## Priorité HAUTE — bugs acpx que casys-trader contourne

### 1. Exit codes structurés (aujourd'hui : parsing textuel de stderr)
`trader/llm.py:144-180` — la classification retryable/non-retryable est une
taxonomie d'heuristiques texte (« 429 », « rate limit », « quota », « internal
error »). Pire cas : **exit ≠ 0 avec stdout+stderr vides** (`llm.py:166-168`),
impossible de distinguer un bug acpx d'un blip provider — traité retryable par
défaut (fix e9d8aef). Upstream : exit codes dédiés (rate_limit=2, quota=3,
internal=4, timeout=5…) et JAMAIS de sortie muette sur erreur.

### 2. Ponts `codex-acp` orphelins (reaping maison)
`trader/llm.py:205-281` — snapshot `ps` avant/après chaque appel + `lsof` cwd
+ SIGKILL ciblé, parce que `codex-acp` (lancé en setsid par acpx) survit à son
parent et s'accumule jusqu'à choker l'app-server (incident 16/06, 26 ponts).
Upstream : le bridge doit mourir avec son parent (waitpid/kill à la fermeture
de session, PR_SET_PDEATHSIG côté Linux).

## Priorité MOYENNE — features/DX

### 3. Label de session natif
`trader/llm.py:123-141` — le « nommage » de session est un préfixe
`[casys-trader:runtime-brain]` injecté DANS le prompt (pollue le contexte
LLM). Upstream : `acpx --session-label <label> exec`, exposé dans
`sessions list`.

### 4. Profil headless « API »
`build_acpx_command` (`llm.py:93-115`) empile toujours les mêmes 4 flags
(`--format quiet --allowed-tools "" --no-terminal
--non-interactive-permissions deny`). Upstream : un flag unique `--headless`.

### 5. Sortie strictement JSON (`--output-schema` / vrais outils)
`codex_client.py:498-504` + `consolidator.py:416-450` — extraction `{...}`
tolérante + réparation de JSON tronqué, car même en `--format quiet` de la
prose peut entourer la réponse. Les contrats de schéma vivent en prose dans le
prompt (pseudo-tools). Upstream, deux étages :
- a minima `--output-format json-strict` (aucune prose, erreur sinon) ;
- idéalement **tool definitions passées à la session** (`--tools-schema
  <file>` ou équivalent ACP) pour remplacer le schema-in-prose — c'est le
  pendant transport du chantier interne `agent_domain_tools` (design
  2026-06-29 : le daemon reste l'exécuteur ; acpx ne ferait que porter les
  définitions/appels).

### 6. Backpressure sous concurrence
Incident 29-30/06 : 3 appels parallèles → overload app-server → erreurs
génériques → `CASYS_DECISION_BATCH_PARALLELISM=1` en mitigation. Upstream :
queue/throttle côté acpx avec erreur 429 propre.

### 7. PATH shims Codex hérités
`trader/process_env.py` — expurge `/codex-path` et
`cryptexd/codex.system` du PATH des enfants. Upstream : acpx/codex-acp ne
devraient pas dépendre de shims de session interactive. (Le filtre `Malloc*`
reste côté casys : générique macOS.)

## Reste côté casys-trader (logique métier, ne pas upstreamer)

- Router multi-tiers spark→sonnet→ollama (`llm.py:509-587`) — la *primitive*
  retry-on-retryable pourrait devenir `--fallback-model`, mais la politique
  reste métier.
- Fail-safe HOLD (`codex_client.py:621-714`), budgets d'appels par cycle
  (`daemon._batch_decide`), REQUEST_CONTEXT/prior_rationale, retries du
  consolidateur, PID file daemon.

## Prochain pas proposé

1. Forker `acpx` sur le GitHub d'Erwan (public OK côté Erwan, sous réserve
   revue sécu du contenu embarqué : pas de tokens/references privées).
2. Premier lot = les 2 bugs HAUTE (exit codes + lifecycle codex-acp) : petits,
   testables, et suppriment ~150 lignes de rustines côté casys-trader.
3. Ensuite label de session + `--headless`, puis json-strict/tools-schema en
   coordination avec le chantier `agent_domain_tools`.
