# How-to — Lire les logs du daemon

> **Type** : How-to (Diátaxis).
> **Fichiers** : `state/daemon_console.log` (texte, niveaux) · `state/events.jsonl` (events structurés)

## Vue rapide dans le terminal

```bash
tail -f state/daemon_console.log        # brut
make logs                               # Gonzo (TUI), daemon supposé lancé
```

`make logs` = `gonzo -f state/daemon_console.log --follow`. Dans Gonzo :
- `/` → filtre **regex** · `Ctrl+f` → filtre **sévérité** (ERROR/WARN/INFO/DEBUG)
- `s` → recherche · `d` → ouvre **Dstl8.Lite** (dashboard web local `http://localhost:5718`,
  heatmaps de sévérité, recherche — 100 % local, sans compte).

Skin Gonzo config : `~/.config/gonzo/config.yml` (`skin: gruvbox` = sombre lisible).

> **Historique vs live** : Gonzo (et le cockpit) chargent tout le fichier → tu vois
> le **scrollback** (vieux logs) en plus du live. Après un changement de niveau,
> les vieilles lignes restent visibles plus haut. Pour repartir propre : archiver
> le log (cf. [run-the-daemon](run-the-daemon.md#relancer-le-daemon)).

## Niveaux de log — `CASYS_LOG_LEVEL`

Défaut **INFO**. La console montre le **signal** : cycles, batchs, décisions de
**trade** (les HOLD sont en DEBUG), gestion de veilles (arm/cancel/expiration),
résumé `[market]`. Le bruit fetch par-symbole (`source_skipped`/`source_fallback`
stale) est en DEBUG.

```bash
# ressortir tout le détail (fetch par-symbole, HOLD détaillés) pour débugger data :
CASYS_LOG_LEVEL=DEBUG   # dans .env, puis relancer le daemon
```

`CASYS_LOG_LEVEL=DEBUG` active tout le DEBUG de `trader.*` (pas les libs tierces —
`ib_async` est musclé à CRITICAL, cf. `runtime/logging_setup`).

## Ce que tu dois voir à l'INFO (repères)

| Ligne | Sens |
|---|---|
| `[config] decision_batch_parallelism=… ` | config au démarrage (vérifiable) |
| `[cycle] start/completed decisions=N executed=N` | bornes de cycle |
| `[market] loaded ok=N / stale symbols=[…]` | chargement marché (résumé) |
| `[batch] deciding symbols=…` puis `[decide] decided=… model_calls=…` | départ du lot puis bilan des appels LLM |
| `[decision …] SYM result action=BUY/SELL …` | **trade** (HOLD → DEBUG) |
| `[watch] armée / annulée / expirée SYM …` | gestion de veilles (visible même en HOLD) |
| `[armed_plan] … réveil planificateur` | plan armé annulé par un garde-fou |
| `[queue.ledger] retry/dead kind=… symbol=… attempt=… delay_s=… next_at=… error=…` | retry durable générique ou tentatives épuisées |
| `news macro retry scheduled/deferred venue=… attempt=… delay_s=… next_at=…` | nouvelle tentative d'un brief macro/news, ou attente normale du backoff |
| `universe retry scheduled/deferred venue=…` | nouvelle tentative du rapport régional Univers |
| `global posture retry scheduled/deferred …` | nouvelle tentative de la posture globale |
| `[learnings_sync] notes=… embedded=… outcomes=…` | ingestion/vectorisation/scoring best-effort terminés |

`scheduled` est émis au moment de l'échec ; `deferred` signifie qu'un tick
ultérieur a respecté `next_at`. Ces lignes sont normales et empêchent justement
les appels LLM de s'enchaîner en boucle. Pour company-micro, la paire
`[queue.ledger]` / `[queue.worker]` porte le symbole et le statut `pending|dead`.

Un événement learnings avec zéro nouvelle note brute peut être un
`feedback refresh` ou un `catch-up` quotidien : lire `curated_candidate_count` et
`curation_due`. Un no-op non dû n'écrit aucun événement de consolidation.

## Events structurés (`events.jsonl`)

Machine-readable, non affecté par `CASYS_LOG_LEVEL`. Lu par le cockpit. Events
notables : `indicator_watch_created/triggered/expired`,
`armed_plan_created/resolved/cancelled/expired`, `watch_cancelled_by_agent`,
`context_resolved`, `cycle_started/completed`.

`cycle_completed` porte un champ additif `stage_timings_ms` (dict plat d'entiers) :
`snapshot_ms`, `gate_scope_ms`, `decide_ms`, `risk_execute_ms`, `record_ms`,
`total_ms`. Un échec de chronométrage omet le champ ; la décision ne change pas.

Usage LLM / latence :

```bash
python -m scripts.llm_cost                 # extrait tokens → state/archive/llm_usage/YYYY-MM.jsonl
python scripts/llm_usage.py cost           # jour×provider + p50/p95 des étages
python scripts/llm_usage.py cost --price-per-mtok grok=3
```

Le job hebdo (`scripts.archive_agent_storage`) lance l'extraction **avant**
d'archiver les sessions ACPX fermées et les sessions Grok inactives, puis de
purger les seuls caches/logs explicitement régénérables. Les configurations,
identités et sessions actives ne sont jamais candidates. Les rollouts Codex et
les sessions Kimi restent protégés tant que leur cycle de vie propre n'est pas
prouvé. Voir [Gérer le stockage des agents et du World Model](manage-storage.md).

## Voir aussi
- [Lancer le daemon](run-the-daemon.md) ·
  [Diagnostiquer les rapports](refresh-and-diagnose-reports.md) ·
  [Maintenir les learnings](maintain-learnings.md) · Architecture §10.1 (logging).
