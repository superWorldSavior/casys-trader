# Plan — Survivabilité du pré-open US (galerie, digest, waiting)

> **Pour les agents :** implémenter lot par lot, TDD, cases à cocher.
> **Décision métier proposée : D19** (à valider).
> **Incident de référence :** pré-open US du 2026-08-18 (~12:00–12:31 UTC).
> **Fact-check :** 4 lectures code + 3 revues adverses + ledger `state/` août.

## 1. Résultat attendu

Un +1 challenger en fenêtre T-90 (DASH, META, NFLX…) reste **légal** (D15).
Il ne doit plus :

1. vider la galerie Reports (dernier succès régional écrasé) ;
2. faire passer un stub `waiting_brief` pour un rapport LLM ;
3. rappeler la posture globale sur un brief écrit pour **une autre liste
   de candidats**.

Si le pré-open courant n’a **pas** de hotlist agent, on en **compose une**
(daemon, ou à la main — lot F). Garder le succès d’hier dans la galerie
n’est pas un substitut : c’est de l’affichage. T-15 n’active pas la liste
d’hier « pour faire semblant ».

## 2. Ce que le fact-check a tranché

### 2.1 Ton intuition (« l’univers rentre tout vite »)

**Nuance.** Le premier scope US arrive bien à T-90 pile (12:00). Ensuite, des
noms **continuent d’entrer**. Août 2026, ledgers `candidate_scopes` :

| Jour | Scopes pré-open US distincts | Nature |
|---|---|---|
| 03, 06, 10, 14, 17 | 1 | Stable |
| 13 | 4 | +WMT (challenger) |
| 18 | ≥5 | +DASH, +META, sticky, +NFLX |
| 11 | 19 | +NVDA puis **oscillation de `fresh_source_refs`** |

3 jours / 8 ont un recast. Ce n’est pas rare. D15 le veut : clôture = parent
top 40 ; T-90 fusionne les challengers overnight **et ceux qui qualifient
encore pendant la fenêtre**.

Le 11 août n’est **pas** le même phénomène que le 18. Avant le 14 août, le
merge challenger était first-wins (`current` avant `retained`) : deux refs
NVDA se relayaient → nouveau `scope_input_signature` toutes les ~2,5 min
(18 `waiting_brief` ce jour-là). Le docstring de
`merge_news_challengers` décrit déjà ce piège. Le fix « plus récent gagne »
est **déjà en prod**. Il reste un trou : à horodatage **égal**, `current`
gagne encore → flip-flop possible.

**Décision de plan :** pas de quiet period temporelle (elle avalerait DASH).
On rend le recast **survivable** (lots A–C) et on ferme le tie-break (lot D).

### 2.2 Contrats déjà écrits, pas tenus

- `universe_runs/latest-<VENUE>.json` « conserve le dernier succès » et
  n’imbrique que `{error, invalid}` sous `latest_failure`
  (`universe_run_store.py`, `universe-rotation.md`, how-to Reports).
- `waiting_brief` **remplace** la projection. La galerie n’a plus de
  `summary` / hotlist ; `_append_failure_warning` ignore cet état.
- Le board exige un brief **exact**. Le digest prend `read_latest` sans
  match. `coverage_changed` (dont `digest_id`) bat `awaiting_brief` →
  posture LLM sur un brief US orphelin (12:01 et 12:06 le 18).
- Page Univers (`universe_pipeline.brief.scope_match`) dit déjà la vérité.
  Reports l’ignore.

### 2.3 « Jointure de scope », en français

Ce n’est pas un truc de base de données. C’est juste : **le brief news
doit avoir été écrit pour la même liste de candidats que le pré-open
courant**.

Chaque recast (DASH entre → nouvelle liste) a un identifiant
`candidate_scope_id`. L’analyste macro tamponne le sien sur le brief.
L’agent régional refuse de choisir la hotlist si les deux IDs diffèrent
(`brief_scope_mismatch`). Ce soir : brief de 12:00 (42 noms) vs liste
de 12:07 (43 noms, +DASH). D’où l’attente, pas un LLM vide.

On **garde** ça sur le chemin automatique : composer la liste de 12:07
avec un brief qui n’a jamais vu DASH, ce serait mentir. Si on a raté
la fenêtre, on ne « recycle » pas la hotlist de 12:00. On **refait**
un brief pour la liste actuelle, puis on compose (lot F).

### 2.4 Ce qu’on ne touche pas

- Chemin auto : brief et liste de candidats doivent porter le même ID
  avant l’appel agent.
- Activer à T-15 une hotlist écrite pour une **autre** liste (celle
  d’hier ou du recast d’avant).
- `latest_failure` / backoff 30→360 min : réservés à `{error, invalid}`.
- Indépendance digest/board/posture vs **succès** régional (un global peut
  tourner dès que les briefs **du bon pack** sont là).
- Gel T-15 : `UNIVERSE_ACTIVATION_WINDOW_MINUTES = 15`.

## 3. Lots

```text
Lot A  store overlay waiting
   ↓
Lot B  galerie (cockpit + desktop)
   ↓
Lot C  digest matching + gate digest-only
   ↓
Lot D  tie-break retain (refs à horodatage égal)
   ↓
Lot F  forcer le régional à la main (+ hotlist manquée)
   ↓
Lot E  docs opérateur
```

A débloque B. C et D sont indépendants de B. F peut suivre A
(le `force=` existe déjà dans le runner, il manque la CLI). E en dernier.

---

### Lot A — Overlay `latest_waiting`

**Fichiers**

- `trader/infrastructure/state_db/universe_run_store.py`
- `trader/runtime/universe_intelligence_runtime.py` (`_record_waiting_once`)
- `tests/state_db/test_universe_run_store.py`
- `tests/runtime/test_universe_intelligence_runtime.py`

**Comportement**

1. Ledger JSONL : chaque `waiting_brief` reste appendé (inchangé).
2. Si `latest-<venue>` est un `success` du même venue :
   projection = `{**success, "latest_waiting": waiting_record}`.
3. Sinon (pas de succès) : `waiting_brief` reste la racine (observable).
4. Un `success` ultérieur efface `latest_waiting` **et** `latest_failure`.
5. `{error, invalid}` : inchangé (`latest_failure`).
6. `_record_waiting_once` déduplique si
   `latest.status == "waiting_brief"` **ou**
   `latest.latest_waiting` a le même `candidate_scope_id` + `error_code`.

**Activation**

`market_rotation_runtime._prepared` lit d’abord `read_prepared` du scope
**courant**. Après overlay, la racine `latest-US.json` peut encore montrer
le succès d’hier : ce n’est pas une hotlist activable aujourd’hui. Le
fallback ne l’utilise pas (autre ID). T-15 sans fichier préparé pour
*cette* liste → `prepare_pending` / baseline déterministe, **sauf** si
le lot F (ou le daemon) a composé entre-temps. Ajouter un test
d’activation : overlay waiting ≠ activation de la liste d’hier.

- [ ] Test store : succès puis `waiting_brief` → racine succès +
      `latest_waiting` ; JSONL a les deux lignes.
- [ ] Test store : `waiting_brief` sans succès préalable → racine waiting.
- [ ] Test store : succès après waiting → plus de `latest_waiting`.
- [ ] Test runtime : deuxième tick même scope/raison →
      `brief_missing_already_recorded` (dédup via overlay).
- [ ] Test activation : `read_latest` succès d’un autre scope + waiting
      overlay → pas d’activation de cette hotlist.
- [ ] Impl store + dédup.

---

### Lot B — Galerie honnête

**Fichiers**

- `trader/interfaces/cockpit/pages/reports_gallery.py`
- `trader/interfaces/cockpit/projections/reports.py`
- `desktop/src/pages/reports.tsx`
- `desktop/bridge/api.py` (si projection structurée)
- `tests/test_cockpit_reports_gallery.py`
- `tests/test_cockpit_reports_projection.py`

**Cockpit**

`_build_regional_detail` :

- chip `success` / `waiting_brief` / `error` / `invalid` (styles déjà dans
  `universe.py` : waiting → warning) ;
- `error_code` (`brief_missing`, `brief_scope_mismatch`) ;
- clés courtes de scope (run vs brief vs `current-<VENUE>`), réutiliser
  `universe_pipeline._brief_pipeline_entry` / `_short_pipeline_id` plutôt
  que dupliquer ;
- si `latest_waiting` : bandeau « pré-open courant en attente · pas un
  rapport vide » **sans** le libellé « dernier essai en échec » ;
- corps = dernier succès, labellé « génération précédente » (`as_of` du
  succès, pas du waiting).

Ne pas étendre `_append_failure_warning` à `waiting_brief`.

**Desktop**

Ne plus dépendre du `pick(summary)` générique pour le régional. Soit :

1. endpoint / champs structurés (`status`, `warning`, `scope_match`,
   `summary`, `previous_generation`) — préféré ;
2. soit clés régionales explicites + bandeau statut.

Référence UX : `desktop/src/components/intelligence/timeline.tsx`
(« Last successful projection retained »).

- [ ] Test détail : `waiting_brief` seul → statut visible, pas le
      placeholder « pas encore de run régional ».
- [ ] Test détail : succès + `latest_waiting` → summary du succès +
      bandeau waiting.
- [ ] Test projection : collector régional expose `latest_waiting`.
- [ ] Impl cockpit.
- [ ] Impl desktop (parity minimale : statut + summary + bandeau).

---

### Lot C — Digest matching + gate digest-only

**Fichiers**

- `trader/runtime/universe_intelligence_runtime.py`
  (`_prepare_global_situation_digest`, `_coverage_requires_refresh`,
  éventuellement `decide_global_posture_refresh`)
- `tests/runtime/test_universe_intelligence_runtime.py`

**Comportement**

1. Digest : même résolution de scope que le board (dernier pré-open, sinon
   current sans phase) + **même** test
   `input_refs.candidate_scope_id == scope.candidate_scope_id`.
   Brief mismatché → venue absente du digest (`venues_seen`).
2. `digest_id` dans `input_coverage` ne bouge plus quand seul un brief
   orphelin change.
3. Defense-in-depth : un changement **digest-only** (ex. brief GLOBAL)
   ne déclenche pas `coverage_changed` si
   `preopen_now ∩ missing_brief_venues ≠ ∅`.
   Un changement de `brief_ids` matching continue de gagner
   (TW swap pendant que US attend).

**Ne pas** placer `awaiting_brief` avant tout `coverage_changed` : ça
bloquerait un vrai brief matching d’une autre venue.

Les tests digest existants sans store de scope doivent recevoir un
fixture scope matching (même helper que `_write_scope_and_brief`).

- [ ] Test : digest exclut un brief scope-mismatché.
- [ ] Test unitaire : `digest_id` change + `missing_brief_venues=["US"]`
      + `preopen_now=("US",)` → `(False, "awaiting_brief:US")`.
      Aujourd’hui ce cas rend `(True, "coverage_changed")` — c’est le
      rouge du 18 août 12:06.
- [ ] Test : brief matching EU/TW qui change pendant US awaiting →
      `coverage_changed` toujours vrai.
- [ ] Test disparition de brief : inchangé
      (`test_decide_refresh_does_not_fire_when_brief_disappears`).
- [ ] Impl filtre digest + gate digest-only.

---

### Lot D — Tie-break retain (pas une quiet period)

**Fichiers**

- `trader/domain/universe/candidate_scope.py`
  (`merge_news_challengers` / `_is_newer_challenger`)
- test unitaire dédié (aujourd’hui seul l’intégration
  `test_tick_preopen_keeps_newer_retained_challenger_when_scout_falls_back_to_older_ref`
  existe)
- éventuellement `tests/test_rotation_venues.py`

**Comportement**

À `latest_published_at` égal (ou timestamps inconnus des deux côtés),
**garder `retained`**. Ne remplacer que si `current` est **strictement**
plus récent. Ça ferme l’alternance A↔B après injection dans le brief
(`_seen_source_refs` fait resurgir l’autre article).

Un article **vraiment plus récent** continue de gagner.

Ne pas retirer `fresh_source_refs` de `_candidate_identity` dans ce lot :
ce serait un second levier, plus rude (un vrai changement de preuve
ref-only ne recasterait plus). À n’ouvrir que si le tie-break ne suffit
pas en live.

- [ ] Test unitaire : current et retained même `published_at`, refs
      différentes → merged = retained ; `scope_input_signature` stable
      sur deux ticks.
- [ ] Test : current strictement plus récent → merged = current.
- [ ] Test existant older-ref : reste vert.
- [ ] Impl tie-break.

---

### Lot F — Forcer le régional à la main

Aujourd’hui : `make macro MARKET=US FORCE=1` existe ;
`docs/how-to/refresh-and-diagnose-reports.md` §5 dit qu’il n’y a **pas**
de commande publique pour l’agent régional. Le runner a déjà
`force=True` (ignore reuse + backoff) mais aucune CLI, et le mismatch
brief/liste bloque toujours avant l’appel.

**But opérateur.** Si on a raté la hotlist du pré-open courant (DASH
entré, brief de 12:00, agent en `waiting_brief`) : en composer une
**maintenant**, pas attendre le prochain jour ni activer celle d’hier.

**Commandes** (miroir macro) :

```bash
make universe MARKET=US FORCE=1
# équivalent
uv run casys-trader universe refresh --venue US --force
```

`MARKET` répétable / `ALL=1` comme `make macro`. `--force` ignore
fraîcheur, cooldown et backoff. **Ne pas boucler.** Même avertissement
que le macro : CLI et daemon ne partagent pas le single-flight — ne
pas lancer pendant une passe Univers déjà visible dans les logs.

**Séquence quand le brief n’est pas celui de la liste actuelle**
(le cas 18 août) :

1. Rafraîchir le macro de la venue pour le scope **courant**
   (équivalent `make macro MARKET=US FORCE=1`).
2. Puis `tick_universe_intelligence(..., venues=("US",), force=True)`.
3. Succès → `universe_prepared/<hash du scope courant>` + projection
   `latest-US.json` en `success` (l’overlay `latest_waiting` disparaît).

`--force` seul ne compose **pas** avec un brief écrit pour l’ancienne
liste. Ça redeviendrait le mensonge « hotlist de 43 noms justifiée par
un brief qui n’a pas vu DASH ». Le chaînage macro→régional est le
défaut de `FORCE=1`. Flag `--no-macro` si le brief courant est déjà
le bon et qu’on veut seulement relancer l’agent.

**Hotlist manquée proche du gong.** Si on entre dans la fenêtre T-15
sans `universe_prepared` pour le scope courant, le runner tente une
composition (brief à jour d’abord, comme ci-dessus) au lieu de se
contenter du `default_hotlist` déterministe. L’échec LLM reste le
fallback radar déjà documenté — on n’invente pas une liste.

**Fichiers**

- `trader/runtime/cli.py` (`universe refresh`, miroir `news-macro refresh`)
- `Makefile` (`universe:` à côté de `macro:`)
- `trader/runtime/universe_intelligence_runtime.py` si le chaînage
  macro ou le last-chance T-15 n’est pas exprimable avec `force=`
  actuel
- `tests/runtime/test_universe_intelligence_runtime.py`
- tests CLI (même style que `news-macro refresh`)

- [ ] Test CLI : `--venue US --force` appelle le tick avec
      `force=True` et `venues=("US",)`.
- [ ] Test : brief d’un autre pack + `--force` (défaut) → macro du
      scope courant puis composition ; `universe_prepared` existe
      pour l’ID courant.
- [ ] Test : `--force --no-macro` + brief mismatché → toujours
      `waiting_brief` / `brief_scope_mismatch`, pas d’appel agent.
- [ ] Test : `--force` + brief déjà bon → ignore backoff/reuse,
      compose, pas de second appel macro.
- [ ] Test T-15 sans préparé : une tentative de composition a lieu
      (pas un skip silencieux vers la seule baseline).
- [ ] `make universe MARKET=US FORCE=1` câblé.
- [ ] How-to §5 réécrit (plus « le daemon seul gouverne »).

---

### Lot E — Docs

**Fichiers**

- `docs/how-to/refresh-and-diagnose-reports.md`
- `docs/reference/universe-rotation.md` (§ échecs, cadence posture, board)
- `docs/architecture.md` (galerie Reports)
- `docs/reference/cockpit.md` si la page Reports est décrite
- `docs/etat-systeme.md` seulement après vérif live, pas dans le PR de code

**Texte à aligner**

- `waiting_brief` / `brief_scope_mismatch` n’est **pas** un rapport vide :
  dernier succès + overlay `latest_waiting`.
- `latest_failure` ≠ attente de brief.
- Posture globale : `coverage_changed` = brief **matching** (ou digest
  de briefs matching). Un digest orphelin ne rappelle pas le LLM.
- Recast pré-open sur +1 challenger = normal ; la galerie reste lisible
  pendant l’attente ; si la hotlist du pack courant manque, on la
  compose (daemon ou `make universe … FORCE=1`).
- How-to §5 : plus « pas de commande régionale » — documenter
  `make universe` / `--no-macro` / ne pas concurrencer le daemon.

- [ ] How-to : snippet `jq` qui montre `status`, `latest_waiting.error_code`,
      ID liste courante vs ID tamponné sur le brief.
- [ ] How-to : `make universe MARKET=US FORCE=1` et le chaînage macro.
- [ ] `universe-rotation.md` : overlay waiting + digest = briefs du
      bon pack.
- [ ] Architecture : galerie = succès + `latest_failure` **ou**
      `latest_waiting`.

## 4. Hors périmètre

- Quiet period / freeze T-90→T-15 qui retarde DASH/META/NFLX.
- Composer la hotlist avec un brief écrit pour **l’ancienne** liste
  (même à la main). On refait le brief, puis on compose.
- Marqueur `status=running` en début de `compose_universe` (lot suivant
  si la galerie ment encore pendant l’appel LLM).
- Refonte générique de `desktop/src/pages/reports.tsx` hors régional.

## 5. Ordre d’implémentation et revue

1. Lot A (store) — un commit, tests store + activation.
2. Lot B (galerie) — un commit, dépend de A.
3. Lot C (digest) — un commit, indépendant.
4. Lot D (tie-break) — un commit, indépendant.
5. Lot F (force CLI + hotlist manquée) — un commit, après A.
6. Lot E (docs) — un commit, après F.

Revue après A+C+F. B et E sont de l’honnêteté opérateur.

Méthode d’exécution (Cursor) : réutiliser overlay / filtre board / CLI
macro / `force=` existants ; DRY-SOLID ; refactors opportunistes seulement
sur le chemin touché ; lots C et D en sous-agents ; un `code-reviewer` sur
le diff ; puis relance superviseur + vérif pré-open US (même
`candidate_scope_id` brief/run, ou overlay `latest_waiting` lisible).

## 6. Critères d’acceptation

Sur un pré-open US qui recaste (rejouer le 18 août en test, ou le prochain
live) :

1. `latest-US.json` garde le dernier `success` + `latest_waiting` tant que
   le brief du **nouveau** scope n’est pas consommé.
2. Galerie US : résumé d’hier/du scope précédent visible, chip waiting,
   scopes `…afe15` vs `…72dc` lisibles. Pas une page `—`.
3. Un brief US mismatché ne change pas `digest_id` assez pour rappeler
   la posture. Un brief US **matching** le fait.
4. Deux ticks scout avec la même news à horodatage égal : un seul
   `candidate_scope_id` pré-open.
5. T-15 n’active pas la hotlist d’un autre pack (succès d’hier sous
   overlay). Si le pack courant n’a pas encore de liste agent : on en
   compose une (daemon last-chance ou `make universe MARKET=US FORCE=1`).
6. `make universe MARKET=US FORCE=1` avec brief périmé : nouveau brief
   pour la liste actuelle, puis hotlist, puis `universe_prepared` de
   **cet** ID. `--no-macro` + brief périmé : pas d’appel agent.

## 7. Preuves et agents

| Axe | Verdict jugé |
|---|---|
| Store aujourd’hui | Overlay seulement `{error, invalid}` ; waiting remplace |
| Activation | `universe_prepared` d’abord ; fallback latest seulement si **même** scope et non-success |
| Galerie | `waiting_brief` = header + `as_of` ; pipeline Univers déjà correct |
| Digest vs board | Digest aveugle ; `coverage_changed` avant `awaiting_brief` |
| Quiet period | Rejetée (D15 + ledgers 13/18 août) |
| Flip-flop 11 août | Vrai historiquement ; fix « plus récent » déjà là ; reste le tie |
| Recast 18 août | Vrais +1 (DASH/META/NFLX), pas du bruit de refs |

Grok-build n’a pas tourné (sandbox / revue auto). Les trois sceptiques
Cursor ont tenu : ne pas mettre waiting dans `latest_failure` ; ne pas
monter `awaiting_brief` au-dessus de tout `coverage_changed` ; ne pas
revendre le merge du 14 août comme travail restant.
