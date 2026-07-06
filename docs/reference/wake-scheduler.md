# Référence — Scheduler, réveils et watches

> **Type** : Reference (Diátaxis).
> **Code** : `trader/planning/scheduler.py`, `trader/application/cycle_schedule.py`,
> `trader/application/watch_scanner.py`, `trader/runtime/cycle_scheduling.py`
> **État** : `state/scheduler.json`
> **Rôle** : source canonique des timers, réveils par symbole, veilles
> indicateur, plans armés et backoff stale.

Cette page fait foi pour le comportement runtime des réveils. Les specs et plans
dans `docs/superpowers/` gardent l'historique de conception ; ils ne sont pas la
source de vérité une fois le comportement livré.

## État persistant

`state/scheduler.json` stocke des **timestamps absolus**, pas des compteurs
process :

| Champ | Sens |
|---|---|
| `default_next_wake` | réveil global par défaut |
| `symbols` | overrides de réveil par symbole (`symbol -> ISO datetime`) |
| `indicator_watches` | watches actives, indexées par `watch_id` |
| `stale_streaks` | streaks de données stale, pour backoff exponentiel |

Le temps continue donc de passer quand l'app est éteinte. Au redémarrage normal,
un symbole dont le timestamp est déjà passé redevient dû ; `--bootstrap` /
`--once` restent les modes explicites qui ignorent les timers et rescannent
l'univers demandé.

## Sélection des symboles dus

`Scheduler.due_symbols(symbols, now)` retourne les symboles dont le réveil est
absent ou passé :

1. si un override par symbole existe dans `symbols`, il prime ;
2. sinon le symbole suit `default_next_wake` ;
3. si aucun wake n'existe, le symbole est immédiatement dû.

`cycle_schedule.select_due_symbols(...)` garde cette règle en mode daemon normal.
En `--once` ou `--bootstrap`, toute la liste de symboles est due.

Quand aucun symbole n'est dû, le daemon n'ouvre pas un cycle vide : il écrit un
status idle et dort jusqu'au plus proche wake borné par le poll runtime.

## `set_next_wake`

Le LLM peut demander une reconsultation du symbole :

| Forme | Effet |
|---|---|
| `{minutes}` / `next_wake_in_minutes` | pose un override absolu `now + minutes` |
| `{on:"session_open"}` | resolve la prochaine ouverture de session du symbole |
| `{on:"macro_event"}` | resolve le prochain événement macro connu |
| `{on:"pre_earnings"}` | réservé, fail-safe si la donnée n'existe pas |
| `{when:<condition>, ttl_minutes?}` | compile une `indicator_watch` de reconsultation (`WAKE`) |

`set_next_wake` signifie toujours **reconsultation** : au réveil, l'agent reprend
la main pour redire HOLD, poser un plan, annuler une veille ou proposer un ordre.

## `indicator_watch`

Une `indicator_watch` est une question posée au marché :
`symbol x indicator x timeframe x op x value`, avec `logic=all|any` et un
`ttl_minutes`.

Invariants runtime :

- les watches sont atomiques : une seule condition rejetée invalide toute la
  watch ;
- tant qu'une watch active existe pour le symbole, le symbole dort hors du cycle
  périodique normal ;
- le `next_wake` du symbole est calé sur l'expiration active la plus proche ;
- le daemon scanne les watches à chaque poll sans appel LLM ;
- si une watch `WAKE` se déclenche, elle est retirée, un event
  `indicator_watch_triggered` est persisté et le symbole reçoit `next_wake = now` ;
- si une watch `WAKE` expire, elle est retirée et le symbole reçoit aussi
  `next_wake = now`, pour que l'agent décide quoi faire du scénario périmé.

Un déclenchement de condition est injecté dans `context.indicator_triggers` du
cycle suivant. Une expiration TTL est injectée dans `context.wake_reasons` avec
`reason=watch_expired`, `watch_id`, `on_trigger`, `expires_at` et `observed_at`.

## Plans armés

`on_trigger=EXECUTE_ORDER` transforme une watch en plan armé : si les conditions
se déclenchent, le daemon exécute l'ordre structuré **sans re-appel LLM**, en
passant quand même par les contrôles déterministes (`exit_plan`, cohérence prix,
RiskGate, broker paper).

Invariants :

- plusieurs plans armés peuvent coexister sur un même symbole ;
- les veilles simples se remplacent par symbole, mais n'écrasent pas les plans
  armés ;
- TTL max plan armé : 240 minutes ;
- si le plan est stale, incohérent, conflictuel ou non exécutable, le daemon
  annule et réveille le planificateur plutôt que d'exécuter en aveugle ;
- à l'expiration, le plan armé est retiré, un event `armed_plan_expired` est
  persisté, le symbole est réveillé immédiatement, et le contexte LLM reçoit
  `context.wake_reasons[]` avec `reason=armed_plan_expired`.

## `exit_watch`

`exit_watch` est une veille attachée à un `TradePlan` ouvert. Elle ne vit pas
comme question de timing générale : elle sert à réveiller l'agent après entrée
si une condition technique post-fill se réalise. Le cooldown par défaut est
15 minutes.

Un trigger `exit_watch` ne soumet pas directement d'ordre ; il signale à l'agent
qu'il doit réexaminer la gestion du plan ouvert.

## Backoff stale

Quand les données marché d'un symbole sont stale, le scheduler utilise un
backoff explicite :

- base : `default_wake_minutes`;
- multiplicateur : `2`;
- cap : `120` minutes ;
- streak persisté et borné dans `stale_streaks`.

Le backoff stale pose un wake par symbole. Comme tout wake par symbole, il est
respecté par le gate de pertinence : à son expiration, le symbole peut revenir
dans le set des dus.

## Observabilité

Surveiller :

| Surface | Ce qu'elle montre |
|---|---|
| `state/scheduler.json` | timers, wakes par symbole, watches et streaks stale |
| `state/events.jsonl` | `indicator_watch_created`, `armed_plan_created`, `indicator_watch_triggered`, `indicator_watch_expired`, `armed_plan_expired`, `exit_watch_triggered` |
| `state/daemon_status.json` | phase courante et prochain sommeil estimé |
| `state/daemon_console.log` | lignes `[watch]`, `[indicator_watch]`, `[exit_watch]`, `[cycle]` |

Voir aussi : [Domain tools](agent-tools.md), [Execution](execution.md),
[Universe rotation](universe-rotation.md), [Lire les logs](../how-to/read-logs.md).
