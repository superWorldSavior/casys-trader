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

clôture venue -> top 40 radar
  -> parent quantitatif immuable (`scope_phase=close`)

pré-open T-90 -> ranking radar rafraîchi + scout fresh-news overnight
  -> top 40 radar ∪ tous les challengers qualifiés
  -> enfant final immuable (`scope_phase=preopen`, parent close tracé)
  -> analyste macro/news async -> brief du même scope final

pool candidat + brief + régime disponible + sticky de contexte
  + GlobalFamilyBoard comparatif TW/EU/US
  -> agent univers async
  -> projection préparée du même scope

pré-open T-15 -> activation exacte ou baseline + fallback_reason
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

### 1.1 Audit shadow du score

Le score de production historique reste `legacy_raw_v1` :

```text
direction = signe(return)
trend = efficiency_ratio * direction
relative_strength = (return - benchmark_return) * direction
score = (w_trend * trend + w_rs * relative_strength)
        * bonus_amplitude * tilt_famille
```

Les poids égaux ne rendent pas les composantes comparables : leurs échelles
brutes diffèrent. De plus, pour un short qui sous-performe son benchmark,
`relative_strength` devient positif et réduit la magnitude négative du trend au
lieu de la renforcer. Ce comportement est désormais mesuré explicitement ; il
n'est pas corrigé silencieusement dans la sélection live.

`balanced_percentile_v1` tourne en **shadow uniquement** : dans chaque venue,
l'efficacité de tendance et la force relative alignée avec la direction sont
converties en percentiles puis combinées à 50/50. L'amplitude conserve son rôle
d'éligibilité mais n'est plus récompensée dans ce contrefactuel. Le classement
actif continue à lire exclusivement `ranked` produit par `legacy_raw_v1`.

Observabilité :

- `state/radar_score_audit.json` : dernier comparatif live, équilibre des
  composantes, anomalie short, overlap et concentration du top 40 ;
- `state/radar_score_bench.json` : replay walk-forward des caches, rendements
  directionnels bruts à 1/3/5 sessions, hit-rate, turnover et concentration ;
- `venue_state.radar_score_audit_observation.selection_effect = "none"` : preuve
  que l'écriture shadow n'a aucune autorité sur la rotation.

Rejouer le bench sans téléchargement :

```bash
uv run python -m trader.reporting.bench.radar_score \
  --config-dir config \
  --cache-dir state/radar_cache \
  --output state/radar_score_bench.json
```

Le bench refuse de recommander automatiquement une bascule. Moins de 60
snapshots uniques reste `insufficient_history`; les rendements sont des proxies
close-to-close bruts, sans coûts ni sizing.

## 2. Scout fresh-news — challengers hors top 40

Le scout lit les news locales récentes au pré-open, donc aussi celles publiées
après la clôture, attribue chaque titre directement à un émetteur et peut faire
entrer un symbole situé sous le top 40 dans le pool final de session.

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

Après la création de l'enfant final au pré-open, le runner d'univers reçoit le
pool candidat, la baseline du parent close, les sticky comme contexte non
retirable, le contexte de marché
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

### 4.1 GlobalFamilyBoard — comparaison, jamais allocation

Avant les trois passes agent, le runtime construit un board commun depuis le
dernier enfant pré-open disponible de `TW`, `EU` et `US` et son brief exact. Pour
chaque famille et chaque venue, il expose le rang radar **dans la venue**, les
comptes candidats/baseline/challengers, les biais, la fraîcheur du scope et au
plus deux observations analyste bornées avec directions, signaux et sources.

Les scores bruts ne deviennent pas un classement global : les familles restent
comparées dans leur contexte de venue. Le contrat du board porte explicitement
`role=comparative_context_not_capital_allocation`. Il ne fixe ni quota de places,
ni capital, ni sizing, et ne fusionne pas les trois agents en un allocateur.
Chaque agent univers demeure seul décideur de sa propre hotlist locale.

Les deux passes LLM sont activées par défaut et peuvent être coupées
indépendamment :

- `CASYS_NEWS_MACRO_ANALYST_ENABLED=0` coupe l'analyste ;
- `CASYS_UNIVERSE_INTELLIGENCE_ENABLED=0` coupe la préparation agent univers.

Ces switches sont fail-open pour le trading : la rotation conserve la baseline.
Sur un état live antérieur à ce pipeline, aucun scope/brief/run n'apparaît
rétroactivement ; il faut un daemon actif, un parent de clôture puis la fenêtre
pré-open de la venue pour matérialiser l'enfant final et les préparations async.

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

Les états TW, EU et US figent à la clôture de leur propre session un parent
quantitatif. Sur la fenêtre pré-open de 90 minutes, le runtime crée un enfant
final `top 40 + challengers overnight`, sans réappliquer hystérésis ni incrémenter
`dwell`. Si les inputs matériels n'ont pas changé, l'identifiant est réutilisé et
aucun nouveau scope n'est appendu. Les runners préparent cet enfant hors boucle.
Dans les 15 dernières minutes, seule la projection portant son
`candidate_scope_id` exact peut être activée, de manière idempotente.
`analyzable_venues()` expose les venues ouvertes ou dans la fenêtre pré-open.
Pendant le chevauchement EU/US, l'univers actif prend l'union des hotlists
concernées ; les sticky d'une venue fermée restent présents.

`state/venue_state.json` garde par venue :

- `candidates` : snapshot courant du pool top 40 + challengers ;
- `scope_phase`, `parent_candidate_scope_id`, `parent_close_at` : phase et
  filiation close -> pré-open ;
- `default_hotlist` : baseline déterministe ;
- `hotlist` : sélection effective après agent ou fallback ;
- `scores`, `dwell`, `last_close_at`, `last_override_at` ;
- les références de dernière activation : scope, run agent, brief et fallback.

`universe.yaml` est réécrit atomiquement seulement si l'ensemble actif change.

## 7. Observabilité et mémoire

Le pipeline conserve six surfaces complémentaires :

- `state/news_challenger_runs/*.jsonl` : chaque run scout, sa couverture partielle,
  ses rejets agrégés, ses challengers et `candidate_run_id` ;
- `state/candidate_scopes/*.jsonl` : parents de clôture et enfants finaux
  pré-open immuables, avec phase, filiation et cache courant par venue ;
- `state/universe_runs/*.jsonl` : attentes, erreurs ou réussite de l'agent,
  `agent_run_id`, brief, couverture et sélection ;
- `state/universe_prepared/<sha256(candidate_scope_id)>.json` : projection exacte
  relue au pré-open ;
- `state/rotation_ledger.jsonl` : activation, sélection finale, sticky,
  `fallback_used` et `fallback_reason`.

Le contexte cross-venue ajoute
`state/global_family_boards/YYYY-MM-DD.jsonl` et sa projection `current.json`.
Un nouveau board n'est appendu que si scopes, briefs ou contenu famille ont
matériellement changé ; chaque run univers conserve son `board_id`, sa couverture
et la référence de persistance.

L'audit radar ajoute deux surfaces sans effet décisionnel :
`state/radar_score_audit.json` pour le shadow live et
`state/radar_score_bench.json` pour le replay offline. Une erreur d'écriture de
l'audit est enregistrée mais ne bloque ni le scope candidat ni la baseline.

Le RAG n'appartient pas au scout et ne compose pas la hotlist. Si la mémoire de
situation est activée en retrieval, elle sera un input historique de l'agent
univers, au même titre que le brief courant. L'agent reste le décideur.

## Fail-safe

- panne scout : top 40 radar seulement ;
- enfant pré-open absent : parent quantitatif utilisé avec
  `fallback_reason=preopen_scope_missing`, sans faire passer le parent pour une
  préparation agent ;
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

Le cockpit expose le `GFB` commun puis une ligne par venue pour `scope → scout → brief → agent →
activation`. Il affiche séparément une réussite agent et un fallback
d'activation, ainsi que provider/modèle et fallback entre backends lorsqu'il y en
a un. Les cinq symboles montrés dans le panneau hot-set sont un aperçu, jamais la
capacité métier de 25.

## Voir aussi

- [News, challengers et analyste](news.md)
- [Architecture de connaissance](agent-knowledge-architecture.md)
- [Spec agent univers](../superpowers/specs/2026-07-09-universe-intelligence-pass-design.md)
- [Registre des décisions métier](../decisions/registre-decisions-metier.md)
