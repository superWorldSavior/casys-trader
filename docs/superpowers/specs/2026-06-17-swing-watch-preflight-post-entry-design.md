# Veille swing, préflight d'exécution et suivi post-entry

**Date** : 2026-06-17
**Statut** : livré 2026-07-02 — briques 0-4 + §13.7 TradePlan + §13.8 calendriers committés (0a7dab3, 8f6f4ea, cfd7025) ; D12 préflight hors cœur V1, en discussion (preflight_reasons mesurable via backtest/plan_replay.py)
**Auteurs** : Erwan + Codex

## 1. Problème

Le passage du profil scalping/intraday vers un profil swing a créé une incohérence :
le daemon utilise encore la fraîcheur runtime `15m` comme verrou global. Si la dernière
barre intraday est stale, le symbole est retiré de `decidable` avant l'appel modèle.
Résultat : le LLM ne peut ni analyser, ni poser une veille, ni préparer un plan, même
quand le contexte daily/swing est suffisant pour réfléchir.

Ce verrou est correct pour l'exécution d'un ordre, mais trop strict pour la veille.
On veut garder le harnais d'exécution strict, tout en autorisant le LLM à travailler
sur des plans swing lorsque le marché est fermé, férié, en week-end, ou simplement
quand Yahoo Finance n'a pas encore publié de barre intraday fraîche.

## 2. Principe

Séparer trois capacités aujourd'hui mélangées par `stale_market_data` :

| Capacité | Autorisée avec runtime stale / marché fermé ? | Garde-fou |
|---|---:|---|
| Analyser / décider une thèse swing | Oui, si daily/session context valide | Contexte marqué non exécutable |
| Poser une veille / planifier un réveil | Oui | TTL, calendrier de marché, logs |
| Exécuter un ordre | Non | Prix runtime frais + session tradable + risk gate |

Le LLM peut donc penser à froid ; le système ne trade qu'à chaud.

## 3. Décisions de design

1. **`stale_market_data` ne doit plus être une muselière LLM universelle.** Il reste
   un signal d'exécution : pas de fill sans prix runtime frais.
2. **Le daily/swing context doit être fetché avant ou en parallèle du verrou runtime.**
   Un symbole runtime-stale mais daily-valide peut devenir `analysis_decidable`.
3. **Proposition D12 : certains plans d'ouverture doivent repasser par un préflight LLM.**
   Cette proposition révise partiellement le comportement D7B actuel, qui exécute
   `EXECUTE_ORDER` sans re-appel modèle. Tant qu'Erwan ne valide pas D12 ou un
   amendement de D7B, D7B reste la règle de production.
4. **Les sorties mécaniques restent déterministes.** Hard stop, TP, trailing,
   profit protection et max-hold peuvent continuer à s'appliquer sans LLM : ce sont
   des garde-fous déjà validés au moment de l'entrée.
5. **Après une entrée, le trade reçoit une veille chaude courte.** Le LLM revoit les
   premières barres exploitables pour vérifier la thèse, ajuster la veille, ou demander
   réduction/sortie.
6. **Le contexte métier du trade doit vivre dans `TradePlan`, pas dans une session LLM.**
   Les sessions acpx restent jetables ; le contexte durable est structuré et réinjecté.

### 3.1 Amendements après review Claude

- **D7B n'est pas modifié implicitement.** Le préflight est une nouvelle décision
  métier proposée (`D12`) : `EXECUTE_ORDER` reste le comportement actuel tant que
  le registre n'a pas tranché.
- **Le préflight ajoute un veto de thèse, pas un veto de risque.** Les plans armés
  ne contournent déjà pas le `RiskGate` déterministe : `check_confidence()` et
  `gate.check()` restent avant `broker.submit()`. Le préflight sert à demander au
  LLM si la thèse d'ouverture est toujours valide avec données fraîches.
- **D11 a déjà corrigé le problème des stops figés.** Le préflight ne doit pas être
  présenté comme la correction principale du postmortem CFR/ASML ; il traite une
  autre faille : l'absence de relecture de la thèse au tir.
- **`WAKE_WITH_ORDER_INTENT` n'est pas un chemin préflight prêt à l'emploi.** Dans
  le code actuel, il sert surtout de dégradation quand le contrat `EXECUTE_ORDER`
  est invalide et se comporte comme une veille. Le chemin armement -> préflight ->
  exécution est donc un vrai chantier.
- **Le TTL actuel des plans armés est court.** `ARMED_ORDER_MAX_TTL_MINUTES=240`
  convient à D7B intraday, mais pas à des setups swing multi-jours sans renouvellement
  explicite ou nouvelle politique de TTL.
- **Point ouvert D12 : préflight conditionnel ou global.** Le choix qui décide si
  D12 aide ou nuit est le critère de déclenchement. La recommandation courante est
  conditionnelle : cross-session / gap d'ouverture, âge d'armement supérieur à un
  seuil, ou `requires_preflight=true` explicite. Un déclenchement intra-séance peu
  après l'armement reste D7B direct tant que le signal mesuré ne justifie pas plus.

## 4. Alternatives considérées

### A. Ne rien changer

On conserve le comportement actuel : runtime stale => pas de modèle. C'est sûr pour
l'exécution, mais incohérent avec du swing. L'agent rate les fenêtres utiles pour
préparer des scénarios avant l'ouverture.

### B. Relâcher simplement le budget de fraîcheur

Passer `15m` à `1h` ou augmenter `DEFAULT_MAX_MARKET_DATA_AGE_MINUTES` réduirait
certains faux stale, mais mélangerait encore analyse et exécution. On pourrait finir
par autoriser des trades sur données trop vieilles.

### C. Séparer analyse et exécution

Recommandé. Le LLM peut analyser et planifier hors marché, mais le daemon bloque toute
exécution tant que la session n'est pas tradable et que le prix runtime n'est pas frais.
Cette option respecte le swing sans affaiblir le harnais de trading.

## 5. Modèle de données proposé

### 5.1 Market context par symbole

Ajouter une synthèse explicite dans le contexte :

```json
{
  "symbol": "2330.TW",
  "execution": {
    "enabled": false,
    "reason": "runtime_stale|session_closed|holiday|weekend|no_price",
    "runtime_interval": "15m",
    "last_runtime_bar_ts": "2026-06-16T13:15:00+08:00",
    "data_age_minutes": 1186.46
  },
  "planning": {
    "enabled": true,
    "reason": "daily_context_fresh",
    "daily_as_of": "2026-06-16",
    "next_session_open": "2026-06-17T09:00:00+08:00"
  }
}
```

`execution.enabled=false` interdit les fills. `planning.enabled=true` autorise
`HOLD`, `next_wake_in_minutes`, `indicator_watch`, plan armé préflight, et notes.

### 5.2 TradePlan enrichi

Pour garder le contexte du trade :

- `entry_thesis` : thèse courte validée par le LLM.
- `entry_decision_id` et éventuellement `preflight_decision_id`.
- `entry_context` : prix, runtime interval, data age, session, daily as-of.
- revue post-entry v1 : réveil scheduler court après fill, puis chemin existant
  `has_position` / `exit_watch` ; le réveil scheduler post-entry contourne le
  debounce de position, sans canal `post_entry_watch` parallèle.
- `last_llm_review` : verdict court `intact|fragile|invalidated`, timestamp.

Le but n'est pas de stocker du raisonnement long, mais de rendre la thèse et les
gardes suffisamment explicites pour les prochains réveils.

`last_llm_review` est persisté dans `TradePlan` puis **réinjecté dans le contexte
LLM par symbole** (`_batch_decide` / `_symbol_facts`) au réveil d'une position
ouverte : le modèle revoit son propre dernier verdict (continuité de thèse). La
persistance métier de ce verdict ne pilote pas le gate de pertinence. Le gate
utilise sa propre dernière revue réelle (`last_llm_at`) : une position ouverte est
revue au plus tard toutes les 2 h, et plus tôt sur réveil agent, trigger explicite
(`exit_watch` inclus), ou transition matérielle de régime/signal HTF. Une transition
inclut apparition, inversion, **disparition**, puis réapparition après revue de
l'absence. `last_llm_at`, les raisons persistantes revues et leurs empreintes sont
upsertés atomiquement dans `casys.db` et hydratés ensemble au restart ; une ligne
legacy ou corrompue est ignorée en bloc afin de forcer une revue fail-open. Le réveil
post-entry court est un réveil agent et passe donc immédiatement.

## 6. Cycle hors marché / runtime stale

```
cycle due
 └─► fetch runtime bars
 └─► classify execution eligibility
 └─► fetch daily/swing context pour les symboles dus, même runtime-stale
 └─► build analysis candidates = due ∩ planning.enabled
 └─► appel LLM avec execution.enabled=false
       ├─ HOLD + next_wake
       ├─ indicator_watch WAKE
       └─ plan armé préflight
 └─► aucune exécution possible
```

Si le LLM demande un `BUY` / `SELL` immédiat alors que `execution.enabled=false`,
le daemon doit bloquer l'ordre avec une raison machine-readable, mais conserver les
éléments non exécutifs valides : veille, wake, learning.

## 7. Préflight avant exécution

Les ouvertures conditionnelles doivent passer par une revue chaude :

```
plan armé froid
 └─► trigger devient vrai OU fenêtre pré-open atteinte
 └─► si prix runtime stale / session fermée : wake pré-open ou attente serrée
 └─► si prix frais + session tradable : appel LLM préflight
       ├─ CONFIRM => risk gate + exécution
       ├─ ADJUST  => nouveau plan / nouvelles conditions
       ├─ CANCEL  => plan supprimé
       └─ DEFER   => next_wake / watch
```

Le mode existant `EXECUTE_ORDER` exécute aujourd'hui directement une ouverture
sans rappel LLM, conformément à D7B. Changer cela demande une décision explicite.
Deux chemins d'implémentation sont possibles :

- câbler réellement `WAKE_WITH_ORDER_INTENT` comme mode standard de préflight,
  avec transmission de l'intention d'ordre jusqu'au prompt et retour `CONFIRM` /
  `ADJUST` / `CANCEL` / `DEFER` ;
- ou garder `EXECUTE_ORDER` mais ajouter `requires_preflight=true` par défaut pour
  les plans swing `OPEN_LONG` / `OPEN_SHORT`.

La recommandation v1 est de créer D12 avec un champ explicite (`requires_preflight`
ou nouveau trigger dédié) plutôt que de changer silencieusement la sémantique de
D7B. Le commentaire actuel du daemon "pas d'appel LLM, pas de gate" doit aussi être
renommé : il parle du gate de pertinence, pas du `RiskGate`.

Le TTL doit être traité dans le même chantier : soit le plan swing se renouvelle
avant expiration, soit le système accepte une durée plus longue mais force une revue
pré-open / pré-exécution.

La variante globale du préflight est volontairement non retenue en v1 : elle risque
de réintroduire l'hésitation LLM sur des plans récents et cohérents, alors que D7B
a été créé pour transformer une délibération validée en exécution réactive. Avant
de câbler D12, mesurer dans les plans historiques combien de déclenchements sont
cross-session / open gap / âgés, puis estimer leur P&L via replay.

## 8. Veille chaude post-entry

Après un fill d'ouverture :

1. créer / enrichir le `TradePlan` avec la thèse et le contexte d'entrée ;
2. poser un réveil court automatique sur la première barre exploitable post-entry ;
3. éventuellement poser une deuxième revue si le trade reste fragile ou très récent ;
4. revenir ensuite au régime normal : `exit_plan`, `exit_watch`, position-open gate.

Le LLM peut alors :

- ne rien changer si la thèse est intacte ;
- ajuster `next_wake` ou poser une `exit_watch` ;
- demander `CLOSE`, `REDUCE` ou `FLIP` si la thèse casse ;
- demander une modification de plan si un canal `plan_update` est ajouté plus tard.

En v1, piloter le trade peut rester limité à `CLOSE` / `REDUCE` / `FLIP` +
veille. Modifier hard stop / TP existants demande un champ explicite nouveau, à
ne pas cacher dans une décision ambiguë.

En v1, ne pas ajouter de canal `post_entry_watch` parallèle : une position ouverte
est déjà sticky via D10 et garantit une revue LLM au plus tard toutes les 2 h via
`has_position`. Les réveils agent, triggers explicites et transitions matérielles
de régime/signal HTF contournent cette cadence. Si un futur champ `post_entry_watch`
explicite est ajouté, il devra devenir sticky aussi.

## 9. Calendrier de marché

Le système actuel gère surtout horaires et week-ends simples par suffixe. Il faut
formaliser un service calendrier :

- suffixes `.TW` et `.TWO` doivent tous deux mapper Taiwan ;
- week-ends : jamais exécutable, mais planning possible ;
- jours fériés / demi-séances : non exécutables sauf calendrier ouvert ;
- `next_session_open` et `last_completed_session` doivent être exposés au contexte ;
- la fraîcheur daily doit se baser sur la dernière séance complétée, pas seulement sur
  un âge brut en minutes depuis un timestamp yfinance à minuit.

V1 peut commencer avec le calendrier existant corrigé (`.TWO`) + week-ends. V2 peut
brancher une librairie de calendriers exchange si nécessaire.

## 10. Invariants

- Aucun ordre d'ouverture sans `execution.enabled=true`.
- Aucun ordre d'ouverture sans risk gate.
- Aucun ordre d'ouverture armé swing marqué `requires_preflight` sans revue LLM
  chaude. Les plans D7B existants restent directs tant que D12 n'est pas validée.
- Les sorties mécaniques validées restent exécutables sans LLM.
- Un `HOLD` infra doit être distinguable d'un `HOLD` LLM, y compris
  `no_decision_in_batch`.
- Le daily stale ne doit pas empêcher l'analyse si la dernière séance complétée est
  encore la bonne séance disponible.
- Un plan ou une position sticky ne doit jamais sortir de l'univers surveillé.
- Une position ouverte en revue post-entry reste protégée par le sticky position D10.
  Si un futur `post_entry_watch` existe sans position, il devra être sticky.

## 11. Tests attendus

- Runtime stale + daily valide => appel LLM autorisé en mode `execution.enabled=false`.
- Runtime stale + LLM demande `BUY` => ordre bloqué, watch/wake conservés.
- Plan d'ouverture cross-session / âgé / `requires_preflight` déclenché => préflight
  LLM appelé avant exécution.
- Plan d'ouverture déclenché mais runtime stale => pas d'exécution, réveil serré / report.
- Plan D7B non préflight => comportement actuel conservé : pas d'appel LLM, `RiskGate`
  déterministe toujours appliqué.
- Hard stop / TP d'un plan ouvert restent mécaniques.
- Fill d'ouverture => `TradePlan` enrichi + revue post-entry planifiée.
- Revue post-entry planifiée => wake symbole court qui contourne le debounce de
  position et force l'appel LLM.
- Position ouverte revue récemment + signal/régime actif qui disparaît => appel LLM
  unique ; après revue de l'absence, pas de boucle ; réapparition identique => nouvel appel.
- Position ouverte jamais revue ou dont la dernière revue réelle date d'au moins
  2 h => LLM appelé même sans signal cockpit.
- `.TWO` utilise le calendrier Taiwan.
- Weekend / férié => planning possible, exécution interdite.
- Daily timestampé à minuit mais dernière séance complétée valide => contexte daily accepté.

## 12. Hors scope v1

- Modifier automatiquement hard stop / TP existants sans canal structuré dédié.
- Calendriers fériés exhaustifs pour toutes les places si une première version suffixe/weekend
  suffit à stabiliser Taiwan.
- Sessions LLM persistantes comme source de vérité du trade. Le contexte durable reste dans
  `TradePlan` et les ledgers.
- Tick-level monitoring. Les revues se font sur barres clôturées disponibles, cohérentes avec
  les données différées.

## 13. Plan d'implémentation pressenti

1. Acter D12 ou amender D7B avant tout changement du chemin `EXECUTE_ORDER`.
2. Corriger l'observabilité : `decision_source` / `model_called` / raisons infra
   explicites pour distinguer infra HOLD et LLM HOLD.
3. Extraire une classification `execution/planning` par symbole.
4. Fetcher le daily/swing context pour les candidats d'analyse même si runtime stale.
5. Autoriser `_batch_decide` sur `analysis_decidable` avec `execution.enabled=false`.
6. Bloquer les ordres immédiats non exécutables tout en appliquant watch/wake.
7. Câbler le vrai préflight des plans d'ouverture (`requires_preflight` ou trigger dédié).
8. Enrichir `TradePlan`, persister `last_llm_review`, **le réinjecter au contexte LLM**
   (continuité de thèse) et ajouter le wake court post-entry via le scheduler existant.
9. Corriger / compléter le calendrier `.TWO`, weekend, puis fériés/session.

## 14. État d'implémentation — 2026-06-17 (traçabilité fait / pas-fait)

Cœur « séparation analyse/exécution » découpé en 5 briques TDD, chacune relue par Codex.

| Brique | Contenu | État |
|---|---|---|
| 0 | Fraîcheur daily « dernière séance complétée » (`assess_daily_freshness`, `last_completed_session_date`) + fix fuseau | ✅ committé (`554a197`, `142141f`), GO Codex |
| 1 (§13.4) | Fetch daily sur **tout l'univers**, plus seulement `tradable_symbols` | ✅ committé (`707c104`), GO Codex |
| 2 (§13.2) | Classification `execution`/`planning` par symbole + injection au contexte LLM | ✅ committé (`27b07b3`, `3d8ecf2`), GO Codex |
| 3 (§13.5) | Garde déterministe : aucun `broker.submit` hors séance (ordres LLM + sorties méca), watch/wake conservés | ✅ committé (`05b160d`), GO Codex |
| 4 (§13.4) | `analysis_decidable` : le LLM analyse les stale-daily-valides (incident Taïwan) + positions stale ; daily fourni au batch ; HOLD synthétique bifurqué | ⏳ **implémenté, 1367 tests verts, NON committé, review Codex en cours** |

**Arbitrage acté (Erwan, 2026-06-17)** : `execution.enabled = runtime frais + prix présent + session.open`.
Gater sur `session.open` (marché fermé ⇒ pas de `submit`, sorties mécaniques incluses) ; FX 24/5 via `session_snapshot`.

### Limitations V1 assumées (à NE PAS laisser en gap silencieux)
- **Cockpit cross-asset** construit sur `tradable_symbols` : un symbole stale analysé n'apparaît pas dans le
  cockpit partagé envoyé au LLM (il a son `per_symbol` mais pas le contexte cross-asset). Sans danger, qualité dégradée.
- **Grâce post-cloche** : juste après la fermeture, le daily du jour n'est pas encore publié → `planning.enabled`
  peut retomber à faux quelques heures (Codex finding non bloquant). À traiter dans le chantier calendrier.
- **Backoff stale** non appliqué aux stale analysés gated (retombent sur le polling par défaut).
- **Cache barres research** (Codex Medium, brique 4) : `analysis_bars_by_symbol` mélange runtime (non-stale)
  et daily (stale), mais `_batch_decide` ne transmet qu'un seul `cached_interval=runtime_interval`. Une
  requête `REQUEST_CONTEXT` sur un stale peut donc refetcher le daily, ou calculer un indicateur `15m` sur
  des barres daily. Edge case (seulement si un stale demande du contexte) ; à clarifier si « daily fourni au
  batch » doit aussi signifier « daily utilisé comme cache ».

### Hors cœur — chantiers séparés à prioriser APRÈS mesure
- **§13.6 préflight D12** : EN DISCUSSION (amende D7B). Replay `preflight_reasons` en place pour mesurer avant câblage.
- **§13.7 enrichir `TradePlan`** : ✅ `last_llm_review` (persisté + réinjecté), `entry_thesis` + `entry_context`
  (peuplés au fill OPEN **et** FLIP), GO Codex. `entry_decision_id` ajouté au schéma mais **non peuplé en V1**
  (nécessite le `sequence` ledger exact, à câbler avec le préflight). `preflight_decision_id` : **PAS fait** (lié au préflight §13.6).
- **§13.8 calendrier fériés/demi-séances MULTI-PLACES** : ✅ **fait** via `exchange_calendars` (offline,
  16/17 places + alias XTAI pour `.TWO`, early closes, lunar TW jusqu'à 2049), GO Codex. Limitation V1 :
  borne temporelle far-future dépendante de l'horloge système (documentée dans `_get_calendar`). **Reste**
  la grâce post-cloche (daily du jour pas encore publié) — non faite.

### Quick wins hors §13 livrés cette session
- `fix(audit)` HOLD infra exclus des métriques D1 (`0e97d55`) ; `fix(rotation)` sticky lit `trade_plans.json` (`7f9dd82`) ;
  `test(market)` calendrier `.TWO` verrouillé (`ed31df3`). **Sticky D10 prod reste à 2 sources sur 4** (plans armés
  scheduler + ordres pending NON collectés) — gap connu, non corrigé.
