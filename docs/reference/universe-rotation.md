# Référence — Gestion d'univers (radar, challengers et agent univers)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/market/radar*`, `trader/market/rotation/`,
> `trader/domain/universe`, `trader/application/universe`,
> `trader/runtime/news_challenger_runtime`,
> `trader/runtime/universe_intelligence_runtime`.
> **Config** : `pool.yaml`, `radar.yaml`, `symbol_news_aliases.yaml`,
> `universe.yaml`.
> **Décisions** : D9, D10, D13, D15.

L'univers live est composé en plusieurs étages. Le radar et le scout construisent
un **pool candidat** ; l'analyste fournit un **brief** ; l'agent univers compose
la **hotlist** ; le code ajoute ensuite les **sticky hors quota**.

## Flux et propriété

```text
pool.yaml
  -> ranking radar éligible complet
  -> top 40 radar

news_items + ranking éligible complet
  -> scout fresh-news
  -> tous les challengers qualifiés hors top 40

clôture venue -> top 40 radar ∪ challengers
  -> pool candidat immuable (`candidate_scope_id`)
  -> analyste macro/news async -> brief du même scope

pool candidat + brief + régime disponible + sticky de contexte
  -> agent univers async
  -> projection préparée du même scope

pré-open venue -> activation exacte ou baseline + fallback_reason
  -> hotlist choisie (<= 25 non-sticky)

hotlist choisie + sticky
  -> composition déterministe
  -> universe.yaml / univers actif
```

Invariants :

- le radar ne compose pas la hotlist ;
- le scout ne compose pas la hotlist ;
- l'analyste ne compose pas la hotlist ;
- **l'agent univers est le seul propriétaire décisionnel des 25** ;
- les sticky sont ajoutés après sa sélection et ne consomment aucune place ;
- le code valide la sortie contre le pool et garde une baseline déterministe en
  fallback si l'agent est indisponible.

## 1. Radar daily — éligibilité et baseline quantitative

Le radar score le grand pool en daily, sans LLM. Il produit un ranking ordonné
avec `symbol`, `attractiveness` et `bias`, après les gardes data/liquidité et les
`hard_exclusions`.

Le radar dit « ce symbole est éligible et quantitativement attractif ». Il ne dit
ni « achète », ni « place dans la hotlist finale ».

Le **top 40** de chaque venue constitue la base peu coûteuse du pool candidat.
Cette borne est distincte de `cap_m` :

- `RADAR_CANDIDATES_TOP = 40` borne uniquement la base radar ;
- `radar.yaml:cap_m = 25` borne uniquement la hotlist non-sticky ;
- aucun cap global ne tronque les challengers qualifiés.

## 2. Scout fresh-news — challengers hors top 40

Le scout lit les news locales récentes, attribue chaque titre directement à un
émetteur et peut faire entrer un symbole situé sous le top 40 dans le pool.

Un challenger doit toujours rester dans le ranking radar éligible complet : la
news contourne l'heuristique de rang, jamais l'éligibilité technique. Il garde sa
provenance, son score, ses `source_refs`, son évidence compacte et son TTL.

Pool final :

```text
candidate_pool = radar_top40 ∪ all_qualified_news_challengers
```

Voir [News, challengers et analyste](news.md) pour les gardes d'attribution,
fraîcheur et observabilité.

## 3. Baseline déterministe et hystérésis

`apply_hysteresis` produit une proposition reproductible de 25 non-sticky
maximum à partir du pool candidat : incumbents, `delta`, `dwell_days` et sorties
d'urgence.

Le champ historique `default_hotlist` représente cette **baseline
déterministe**, pas le propriétaire de la hotlist finale. Elle sert à :

- backtester la rotation ;
- mesurer l'alpha des changements de l'agent ;
- fournir un fallback explicite si l'agent univers échoue.

Un ancien incumbent absent du nouveau pool `top40 + challengers` ne survit pas
par inertie.

## 4. Agent univers — préparation et composition de la hotlist

Après la clôture de chaque venue, le runner d'univers reçoit le pool candidat, la
baseline, les sticky comme contexte non retirable, le contexte de marché
**lorsqu'il est disponible**, et la projection bornée du brief macro/news. Il
compose la hotlist effective hors de la boucle synchrone du daemon.

Le contrat nominal rend la liste complète :

```json
{
  "selected_hotlist": ["AAPL", "DE"],
  "summary": "...",
  "family_postures": {"us_mega_tech": "constructive"},
  "symbol_rationales": {"AAPL": "...", "DE": "..."}
}
```

Le runtime dérive encore `add/remove` contre la baseline pour le ledger et garde
un ancien chemin synchrone delta uniquement pour compatibilité/tests/urgence.
Ce delta n'est pas accepté comme réponse nominale du nouvel agent : celui-ci doit
rendre la liste complète, une rationale par symbole et une posture pour chaque
famille sélectionnée. La préparation vérifie le
`candidate_scope_id`; au pré-open, l'activation vérifie ensuite :

- ajout uniquement dans le pool candidat ;
- aucun sticky dans les 25 places ;
- résultat limité à 25 non-sticky ;
- brief et projection non expirés, associés au scope exact.

Le prompt contient, pour chaque challenger, le score fresh-news, la date, les
types d'événement, le headline et le publisher. Il reçoit aussi les zones,
familles, symboles candidats, alertes, sources et métadonnées de couverture du
brief courant ; les `input_refs` bruts sont exclus. Le retrieval historique de
`situation_memory.db` reste `not_enabled`. Le contexte régime vient de
`state/last_regime.json`, snapshot atomique typé produit par le cycle symbole.
Sa couverture est explicitement `active_tradable_universe`, donc partielle pour
les nouvelles familles candidates ; après 96 h il est marqué stale et ses valeurs
ne sont plus injectées. L'attractivité moyenne, le biais, le statut news et le
statut régime de chaque famille restent visibles séparément.

Les deux passes LLM sont activées par défaut et peuvent être coupées
indépendamment :

- `CASYS_NEWS_MACRO_ANALYST_ENABLED=0` coupe l'analyste ;
- `CASYS_UNIVERSE_INTELLIGENCE_ENABLED=0` coupe la préparation agent univers.

Ces switches sont fail-open pour le trading : la rotation conserve la baseline.
Sur un état live antérieur à ce pipeline, aucun scope/brief/run n'apparaît
rétroactivement ; il faut un daemon actif et la prochaine clôture de la venue pour
matérialiser le premier `candidate_scope_id`, puis les préparations async.

## 5. Sticky — composition après la décision agent

Les sticky protègent les obligations déjà ouvertes : positions, plans et autres
gardes runtime disponibles. Ils sont collectés avant le recalcul pour ne pas
consommer une place de la hotlist, puis ajoutés après la sélection :

```text
univers_actif = selected_hotlist ∪ sticky
```

Le total peut donc dépasser 25. Un sticky peut apparaître dans le ranking ou le
pool pour information, mais il ne devient jamais l'une des 25 places par ce seul
fait.

## 6. Rotation par venue et univers actif

Les états TW, EU et US sont recalculés à la clôture de leur propre session. Cette
clôture persiste un scope immuable ; les runners préparent ensuite hors boucle. Au
pré-open de la même venue, seule la projection portant le même
`candidate_scope_id` peut être activée, une fois de manière idempotente.
`analyzable_venues()` expose les venues ouvertes ou dans la fenêtre pré-open.
Pendant le chevauchement EU/US, l'univers actif prend l'union des hotlists
concernées ; les sticky d'une venue fermée restent présents.

`state/venue_state.json` garde par venue :

- `candidates` : snapshot courant du pool top 40 + challengers ;
- `default_hotlist` : baseline déterministe ;
- `hotlist` : sélection effective après agent ou fallback ;
- `scores`, `dwell`, `last_close_at`, `last_override_at` ;
- les références de dernière activation : scope, run agent, brief et fallback.

`universe.yaml` est réécrit atomiquement seulement si l'ensemble actif change.

## 7. Observabilité et mémoire

Le pipeline conserve cinq surfaces complémentaires :

- `state/news_challenger_runs/*.jsonl` : chaque run scout, sa couverture partielle,
  ses rejets agrégés, ses challengers et `candidate_run_id` ;
- `state/candidate_scopes/*.jsonl` : snapshot immuable de clôture et
  `candidate_scope_id`, plus cache courant par venue ;
- `state/universe_runs/*.jsonl` : attentes, erreurs ou réussite de l'agent,
  `agent_run_id`, brief, couverture et sélection ;
- `state/universe_prepared/<sha256(candidate_scope_id)>.json` : projection exacte
  relue au pré-open ;
- `state/rotation_ledger.jsonl` : activation, sélection finale, sticky,
  `fallback_used` et `fallback_reason`.

Le RAG n'appartient pas au scout et ne compose pas la hotlist. Si la mémoire de
situation est activée en retrieval, elle sera un input historique de l'agent
univers, au même titre que le brief courant. L'agent reste le décideur.

## Fail-safe

- panne scout : top 40 radar seulement ;
- panne analyste : brief précédent actif ou contexte explicitement absent ;
- brief absent ou d'un autre scope : `brief_missing` / `brief_scope_mismatch`,
  aucune sélection préparée recyclée ;
- panne retrieval/RAG : composition sans mémoire historique ;
- projection absente, pending, invalide ou expirée : `fallback_reason` dans le
  ledger et `default_hotlist` déterministe ;
- un fallback pending/missing n'est pas terminal : le lecteur réessaie et peut
  activer un run tardif ; les répétitions identiques ne dupliquent pas le ledger ;
- panne agent univers : `default_hotlist` déterministe ;
- aucune de ces pannes ne retire les sticky.

Le cockpit expose une ligne par venue pour `scope → scout → brief → agent →
activation`. Il affiche séparément une réussite agent et un fallback
d'activation, ainsi que provider/modèle et fallback entre backends lorsqu'il y en
a un. Les cinq symboles montrés dans le panneau hot-set sont un aperçu, jamais la
capacité métier de 25.

## Voir aussi

- [News, challengers et analyste](news.md)
- [Architecture de connaissance](agent-knowledge-architecture.md)
- [Spec agent univers](../superpowers/specs/2026-07-09-universe-intelligence-pass-design.md)
- [Registre des décisions métier](../decisions/registre-decisions-metier.md)
