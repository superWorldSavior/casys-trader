# Rotation swing-aware + réactivation du sélecteur LLM

**Date** : 2026-06-17
**Statut** : design validé (Erwan, 2026-06-17) — prêt pour plan d'implémentation TDD
**Auteurs** : Erwan + Claude (+ consult Codex)
**Décision métier** : D13 (registre `docs/decisions/registre-decisions-metier.md`)

## 1. Problème — un mismatch entre deux mécanismes corrects

Deux mécanismes committés, chacun correct pris seul, donnent un **sens opposé** au
même artefact `config/universe.yaml` :

- **Rotation D9/D10** (`trader/rotation_venues.py`) : `universe.yaml` = « ce qu'on
  **trade maintenant** ». `compose_active_universe(state, open_venues, *, sticky)`
  (`rotation_venues.py:65`) ne lit que les hotlists des venues présentes dans
  `open_venues()` (`rotation_schedule.py:55`, fenêtre `open ≤ now < close`). Marché
  fermé ⇒ hotlist jamais lue ⇒ symboles non-sticky **éjectés**. But explicite :
  économiser les appels LLM du hot-path 15m.
- **Swing briques 0-4** (`daemon.py:run_cycle`, design
  `2026-06-17-swing-watch-preflight-post-entry-design.md`) : `universe.yaml` = « ce
  sur quoi on peut **réfléchir** ». Brique 4 (`_analysis_eligible`, `daemon.py:1690`)
  veut analyser à froid les marchés fermés/stale daily-valides pour préparer la séance.

Le daemon écrit `universe.yaml` (`_rotation_tick`, `daemon.py:~2706`) **avant** de le
relire et de lancer `run_cycle`. Donc un marché fermé **sans position** (les candidats
qu'on voudrait pré-analyser avant le gong) n'entre jamais : `run_cycle` ne le voit pas.
Seuls les **sticky** (position/plan ouvert) survivent. La brique 4 ne rattrape que les
stale **déjà** dans l'univers.

Second constat, lié : le **sélecteur LLM** de symboles voulu par D9 est **dormant**.
`config/radar.yaml:19 override_enabled: true`, mais le flag n'est lu que par `run_cli`
(`rotation_wiring.py:334`), chemin **D9** appelé seulement par `maybe_rotate`
(`rotation_daemon.py:36`, jamais appelé par le daemon) et le CLI `python -m
trader.rotation` (`rotation.py:413`, non câblé). Le chemin **vivant** `rotation_venues.tick`
(`rotation_venues.py:177`) n'a **aucun hook override** — rotation 100 % déterministe
(radar + hystérésis). Oubli lors du passage D9 → D10. Et même quand l'override tournait,
son prompt (`build_override_prompt`, `rotation_override.py:17`) ne recevait que
`ranked` + `default_hot`, **jamais les sticky** (protégés seulement post-hoc par
`apply_override`, `rotation.py:145`).

## 2. Principe directeur

`universe.yaml` est **réétiqueté** : il devient l'**univers de surveillance active**
(« ce qu'on surveille/analyse maintenant »), pas l'univers tradable. L'exécutabilité
reste décidée **au runtime, par symbole** par brique 2 (`classify_symbol_context`,
`trader/tools/market.py:613`) et appliquée par brique 3 (`_execution_blocked_reason`,
`daemon.py:586` ; gardes `daemon.py:2086` + `771`). Conséquence : **aucun statut
tradable n'est persisté dans le fichier** — le contenir suffit, le runtime gate.

Admettre un marché fermé dans l'univers est **sûr** : brique 3 (`execution.enabled =
prix frais AND session_open AND non-stale`, `market.py:647`) bloque déjà tout
`broker.submit` hors séance, ordres LLM **et** sorties mécaniques. Le seul coût
d'admettre, c'est des appels LLM et du churn scheduler — bornés par la fenêtre pré-open.

## 3. Volet A — Appartenance swing-aware (fenêtre pré-open, déterministe)

`open_venues(now)` est élargi en `analyzable_venues(now) = open_venues ∪ preopen_venues`,
où `preopen_venues` = les venues dont la **vraie prochaine ouverture** est dans
`preopen_window_minutes` (défaut **90 min**, configurable). `compose_active_universe`
consomme `analyzable_venues` au lieu de `open_venues`.

- À l'ouverture, la venue passe `preopen → open` **sans quitter le fichier** ⇒ **zéro
  churn** `reconcile_universe` (`scheduler.py:697`).
- À la clôture, la venue disparaît (sauf sticky) ⇒ l'objectif coût de D9/D10 est préservé
  (on ne paie pas le hot-path la nuit entière).
- Le daily ne bouge pas la nuit ⇒ réfléchir 90 min avant le gong capture l'essentiel de
  la valeur swing, avec moins de stale-thesis qu'une veille toute la nuit.

`analyzable_venues` doit calculer la prochaine ouverture via la **même source** que
`session_snapshot` (le service `exchange_calendars` de §13.8 : DST, demi-séances,
fériés, week-ends longs), sinon on déplace le bug au lieu de le corriger.

**Non retenu** : (A-bis) admettre les fermés en continu toute la nuit — recrée le coût
D9/D10, sans valeur (daily immobile). (B) deux fichiers univers exécution/analyse —
races concrètes (double `reconcile_universe`, sticky dédoublé, watch froide purgée par
l'univers d'exécution, fichiers désynchronisés) ; utile seulement avec une vraie passe
research séparée (news/earnings/week-end/macro/multi-jours) — cible ultérieure, hors
scope.

## 4. Volet B — Réactivation du sélecteur LLM (défaut déterministe + override tracé)

On réactive le sélecteur LLM voulu par D9, **sur le chemin vivant D10**, en **préservant
le défaut déterministe** (D9 décision verrouillée n°1 : la backtestabilité de la rotation
*est* la stratégie).

Séquencement par venue :

1. **À la clôture de SA session** (mécanisme `due_venues` existant) : `update_venue_ranking`
   produit le **hotlist par défaut déterministe** (radar score + hystérésis). C'est la
   **baseline backtestable**, inchangée. Données EOD complètes.
2. **En pré-open** (nouvelle fenêtre du volet A, 1 appel/venue/jour ≈ 3/jour) : appel LLM
   **override** sur le défaut de cette venue, avec contexte frais. `apply_override`
   (`rotation.py:121`, protège déjà les sticky) produit le **hotlist final** qui entre
   dans `universe.yaml`. Échec/timeout LLM ⇒ **fail-safe** : on garde le défaut déterministe.
3. `rotation_ledger` (défaut vs final) **mesure l'alpha** ajouté par le LLM vs la baseline.

### 4.1 Enrichir le prompt du sélecteur (contrat élargi)

`build_override_prompt` (`rotation_override.py:17`) et le payload (`rotation.py:320`,
aujourd'hui `{ranked, default_hot}`) reçoivent en plus :

- **`sticky`** : l'ensemble des symboles non-dégradables (positions + plans + watches +
  pending). L'agent **voit** ce qu'il ne peut pas retirer ⇒ il ne sélectionne plus en
  aveugle (anti-pattern condamné : « armer en aveugle = doublons », cf chantier OCO).
- **Contexte de marché** : régime sectoriel (D2 `family_regime`), santé des familles,
  éventuellement état des autres marchés. Champs additionnels à étendre plus tard ;
  v1 = au minimum `sticky` + régime sectoriel disponible.

### 4.2 Câblage sur D10

Le chemin D10 (`tick` / `update_venue_ranking`) n'a pas de hook override. Ajouter une
étape override **pré-open par venue** : construire `override_fn` via `build_llm_override_fn`
(`rotation_wiring.py:244`) si `override_enabled`, l'appliquer au défaut de la venue avec
`apply_override`, fail-safe sur le défaut. `override_enabled` reste lu (on **ne flippe
pas** le flag : on le branche enfin sur le chemin vivant). Le chemin D9 legacy
(`run_cli`/`maybe_rotate`) peut être laissé tel quel ou supprimé en nettoyage séparé
(hors scope ; à tracer pour éviter le footgun d'un double chemin).

### 4.3 Pas de « LLM choisit sur tout le pool sans shortlist »

Le pool fait **431 symboles** (US 246, EU 133, TW 52). Passer 431 stats au LLM = coût
tokens élevé + discrimination faible au-delà de ~30-40 items. La shortlist radar n'est
**pas une cage** sur le LLM : c'est un **compresseur 431→N déterministe** qui est *aussi*
la baseline backtestable. Le LLM choisit finement sur le candidat-set classé, pas sur le
pool brut.

## 5. Garde-fous critiques (relevés Codex)

1. **Sticky AVANT que la venue ressorte.** Un plan/watch créé en analyse froide pré-open
   doit devenir **sticky** avant que la venue quitte la fenêtre (clôture), sinon
   `reconcile_universe` (`scheduler.py:697`) le purge. ⚠️ Piège n°1.
2. **Dû au scheduler avant l'ouverture.** Un symbole admis en pré-open doit être
   effectivement **dû** (`due_symbols`, `scheduler.py:997`) avant l'ouverture, sinon il
   est présent mais jamais relu — présent pour rien.
3. **Overlap artificiel + cap.** Pré-open d'un marché pendant qu'un autre est ouvert crée
   un overlap nouveau ⇒ l'ordre des symboles et `max_model_calls_per_cycle`
   (`daemon.py:647`, défaut 25) comptent.
4. **Calendrier = source unique.** `next_session_open`, DST, demi-séances, fériés,
   week-ends longs viennent de la **même source que `session_snapshot`**
   (`exchange_calendars`, §13.8). Sinon le volet A déplace le bug.

## 6. Invariants

- Aucun ordre exécuté hors `execution.enabled=true` (brique 3 inchangée).
- Un marché admis en pré-open est analysable (`planning.enabled`) mais **non exécutable**
  jusqu'à `session_open`.
- À l'ouverture, la venue ne **sort jamais** de l'univers (preopen→open continu, churn nul).
- Un plan/watch/position sticky n'est **jamais** purgé par la rotation (D10 invariant
  préservé).
- L'override LLM ne peut **jamais retirer un sticky** (`apply_override` rejette,
  `sticky_protected`).
- Échec/timeout LLM ⇒ **défaut déterministe** conservé (fail-safe ; rotation jamais bloquée).
- `rotation_ledger` enregistre défaut vs final à chaque override (mesure d'alpha).
- Le prompt du sélecteur **contient toujours** le set sticky (jamais de sélection en aveugle).

## 7. Tests attendus

Volet A :
- Venue fermée mais dans la fenêtre pré-open ⇒ ses symboles présents dans
  `compose_active_universe`.
- Venue fermée hors fenêtre ⇒ absente (sauf sticky).
- Transition preopen→open ⇒ univers inchangé pour ces symboles (zéro churn / aucun
  `reconcile_universe` purge).
- `analyzable_venues` utilise le calendrier exchange (férié/demi-séance/week-end :
  prochaine vraie ouverture).
- Symbole admis pré-open ⇒ `due_symbols` le rend dû avant l'ouverture.

Volet B :
- `override_enabled=true` sur D10 ⇒ override appelé en pré-open, final ≠ défaut possible,
  `rotation_ledger` trace les deux.
- Échec LLM ⇒ final = défaut déterministe (fail-safe).
- Override tente de retirer un sticky ⇒ rejet `sticky_protected`, sticky conservé.
- `build_override_prompt` contient le set sticky + le contexte régime.
- `override_enabled=false` ⇒ rotation 100 % déterministe (défaut), aucun appel LLM.

## 8. Hors scope v1

- Suppression du chemin D9 legacy (`run_cli`/`maybe_rotate`) — nettoyage séparé tracé.
- Passe research swing séparée / deuxième univers (volet B du mismatch) pour
  news/earnings/week-end/macro/multi-jours.
- Contexte de sélection exhaustif (corrélations inter-marchés, news) — extensions futures
  du prompt au-delà de `sticky` + régime sectoriel.
- Modification des caps par venue / calibration de `preopen_window_minutes` au-delà du
  défaut 90 min.

## 9. Points de câblage (récapitulatif file:line)

| Élément | Emplacement |
|---|---|
| `compose_active_universe` (consomme `analyzable_venues`) | `rotation_venues.py:65` |
| `open_venues` → `analyzable_venues` | `rotation_schedule.py:55` |
| `tick` (ajouter hook override pré-open) | `rotation_venues.py:177` |
| `update_venue_ranking` (défaut déterministe) | `rotation_venues.py:226` |
| `apply_override` (protège sticky, à réutiliser) | `rotation.py:121` |
| `build_override_prompt` (ajouter sticky + contexte) | `rotation_override.py:17` |
| payload override (ajouter sticky + contexte) | `rotation.py:320` |
| `build_llm_override_fn` / `override_enabled` | `rotation_wiring.py:244,334` |
| `sticky_collector` | `rotation_collectors.py:21` |
| `reconcile_universe` (churn / purge) | `scheduler.py:697` |
| `due_symbols` | `scheduler.py:997` |
| brique 2/3 (gate runtime) | `market.py:613,647` ; `daemon.py:586,2086,771` |
| calendrier (next open) | `exchange_calendars` via `session_snapshot` (§13.8) |
| `rotation_ledger` (mesure défaut vs final) | module `rotation_ledger` |
