# Univers régime-adaptatif + analyse individuelle enrichie — design

> **Statut courant — ⛔ SUPERSÉDÉ par D15 (2026-07-10).**
> Ce document est conservé intégralement comme historique de conception. Sa
> proposition de `cap_effective` faisant varier la largeur/hotlist entre un
> plancher et un fusible, ainsi que son étiquette « D15 proposée », ne sont plus
> normatives. Le contrat courant sépare désormais : pool candidat = top 40 radar
> + tous les challengers fresh-news qualifiés ; hotlist = 25 non-sticky maximum,
> propriété de l'agent univers ; sticky ajoutés ensuite hors quota.
>
> Les briques `last_regime` et analyse individuelle restent des pistes distinctes
> de backlog : elles ne sont ni rejetées ni validées par D15. Le bloc ci-dessous
> conserve le statut historique original du 2026-07-05.
>
> **Statut : 🧭 DESIGN — à valider avec Erwan (2026-07-05).** Cadré depuis
> l'état réel du code (carto fichier:ligne au §3). Trois briques partageant un
> **carburant commun** — le régime sectoriel D2 — débloqué par un fix trivial.
>
> Décision proposée : **D15** *(à confirmer : le registre s'arrête formellement à
> D14 ; une note mémoire évoque un « D15 exploration » officieux → trancher le
> numéro avant d'écrire dans `registre-decisions-metier.md`)*.
>
> S'appuie sur : **D9** (radar/rotation deux niveaux), **D10** (hot-lists par
> venue), **D13** (override LLM réactivé + `analyzable_venues`), **D7**
> (`relevance_gate`), **D2** (`family_regime`), et le chantier task-ledger Phase 3
> (`docs/superpowers/specs/2026-07-04-phase3-decide-execute-via-file-design.md`,
> grain-symbole `CASYS_QUEUE_DECIDE_ENABLED`).

## 1. Objectif — la thèse

Le système fait aujourd'hui **deux compromis de coût aveugles** :

1. **Largeur bridée** — `cap_m = 25` fixe par venue (`radar_config.py:13`),
   indépendamment de l'opportunité réelle du marché. On surveille 25 quoi qu'il
   arrive : trop en marché haché (25 faux positifs momentum faibles), **pas assez
   en régime porteur large** (on rate l'opportunité).
2. **Profondeur compressée** — la décision LLM regroupe les symboles par chunks
   (`batch_size = 5`, `planner_batch.py:20`) dans un même appel comparatif. Et le
   mode grain-symbole qui existe déjà (Phase 3, `decide_one`) est **dégradé** :
   `allow_context_request=False, allow_tool_calls=False`, pas de `recall_learnings`
   (spec phase3 §7). L'individuel actuel analyse *moins bien* que le batch.

On remplace ces deux compromis aveugles par deux mécanismes **pilotés par le
signal** :

- **Largeur = f(opportunité)** — l'univers de surveillance s'ouvre quand il y a du
  régime/de l'attractivité, se resserre sur le bruit. **Jamais bridé par une
  politique** : le seul plafond est un fusible de sécurité anti-runaway.
- **Profondeur individuelle enrichie** — 1 appel LLM par symbole avec le contexte
  *complet* (tools + recall + context_request) et un budget dédié, plus profond
  qu'un slot de batch.

Le liant des deux est le **régime sectoriel D2** (`family_regime`), déjà calculé
chaque cycle mais jamais rendu disponible à la phase rotation. Le débloquer est un
fix de ~10 lignes qui alimente *les deux* leviers.

> Principe directeur (Erwan) : **on ne se limite pas en symboles s'il y a de
> l'opportunité.** Le coût suit le signal ; on dépense les tokens là où il y a
> quelque chose à trader, pas en rabotant uniformément.

## 2. Décisions validées (2026-07-05)

| # | Décision | Choix |
|---|---|---|
| 1 | **Rôle du cap** | **Fusible seul.** `cap_effective = f(opportunité)`, non plafonné par une politique ; borné uniquement par un fusible de sécurité (`SAFETY_CAP ≈ 100`) anti-runaway/anti-bug, et un plancher bas (`FLOOR ≈ 8`) pour toujours surveiller le haut du panier. Marché haché → ~8 ; lisible → ~25 ; risk-on large → 60/80… |
| 2 | **Signal d'opportunité** | **Régime D2 + attractivité radar combinés.** Régime = nb de familles de la venue avec `frac ≥ seuil` ; radar = nb de symboles de la venue avec `attractiveness ≥ seuil`. La largeur monte si l'un **ou** l'autre est riche. |
| 3 | **Profondeur individuelle** | **Plus profond que le batch.** `decide_one` enrichi : `recall_learnings` ON, tool round / `context_request` ON, budget tokens/tour dédié, multi-tour possible sur signal fort. L'individuel devient meilleur qu'un slot de batch. |
| 4 | **Déterminisme préservé** | Le cap adaptatif est une **fonction pure** (backtestable) — respecte D9 lock n°1 (le défaut reste reproductible ; le LLM n'est qu'override). |
| 5 | **Gate D7 conservé** | `relevance_gate` reste le filtre de *pertinence par cycle* (qui appelle le LLM maintenant). Cap adaptatif = *largeur de surveillance* ; gate D7 = *pertinence événementielle*. Deux étages distincts (cf §4.4). |

## 3. État des lieux (carto code, source de vérité)

### 3.1 Le cap aujourd'hui
- `RadarParams.cap_m = 25` (`radar_config.py:13`), lu de `config/radar.yaml:2`.
- Appliqué **par venue à la clôture** : `tick()` → `run_venue_close(..., cap_per_venue=params.cap_m)` (`venues.py:257-267`) → `update_venue_ranking(..., cap_per_venue=cap)` (`venues.py:374`) → `apply_hysteresis(..., cap_m=cap)` (`venues.py:375`).
- `apply_hysteresis` (`core.py:19-78`) borne à `cap_m` et protège les incumbents ≥ `dwell_days` (pas d'éviction sous dwell — `core.py:69`).

### 3.2 Le régime D2 aujourd'hui
- `compute_family_bias` (`family_regime.py:44-79`) retourne par famille :
  `{"dir": "up"|"down", "frac": majority/n, "up": int, "down": int, "n": int}`.
  `frac = round(max(up,down)/(up+down), 2)` — 1.0 = unanimité (`family_regime.py:72`).
- **Calculé chaque cycle** dans la phase décision : `daemon.py:1438-1444`, sur les
  daily bars (`lookback_bars=3` ≈ 3 séances), injecté dans le `shared_context` du
  batch (`regime_families`).
- Seuil « famille forte » = `frac ≥ 0.70` (`infra_holds.py:58`, utilisé par le gate D7).

### 3.3 `last_regime.json` — le pont manquant
- **Lu** : `daemon.py:3127-3132` → `build_market_context_from_regime` (`wiring.py:281`)
  → passé à `tick()` comme `market_context` pour l'override de sélection pré-open.
- **Écrit : nulle part.** Recherche exhaustive = 0 écriture (confirmé
  `registre-decisions-metier.md:230`). Donc `_market_context = None` en prod →
  l'override LLM (`override.py:61`) tourne **sans** la section régime → « sélection
  en aveugle » que D13-B prétendait supprimer.

### 3.4 La décision LLM aujourd'hui
- **Mode batch legacy** : univers → gate → `decidable` → chunks de `batch_size=5`
  (`planner_batch.py:267`), parallélisme 3, bornés par `max_model_calls_per_cycle=25`
  (`daemon.py:2720`, `planner_batch.py:281`). Réponse démultiplexée par
  `parse_batch` (`parsing.py:542-574`, isolation par symbole).
- **Mode grain-symbole** (`CASYS_QUEUE_DECIDE_ENABLED`, activé en paper 04/07) :
  1 tâche `decide`/symbole (`decide_one.py`), mais **dégradé** — `decide_batch(
  allow_context_request=False, allow_tool_calls=False)`, pas de recall (spec phase3 §7).
- **Gate D7** (`relevance_gate.py:19-47`) : un symbole passe au LLM si
  `agent_wake ∨ trigger ∨ position ∨ family_regime_strong(frac≥0.70) ∨ signal ∨
  periodic_review(≥4h)`. Sinon HOLD infra (pas d'appel).

### 3.5 Le shared context porte déjà la comparaison
Le `shared_context` (`daemon.py:1400-1445`) contient `regime_families` **et**
`cockpit` (barres+indicateurs de tout l'univers). La « vue comparative
cross-symbole » n'a donc pas besoin de N symboles dans un même prompt : elle est
déjà portée, de façon structurée, par le régime D2 et le cockpit. Argument clé
pour l'individuel (cf §4.3 et spec phase3 §4.3bis).

## 4. Architecture

### 4.1 Brique 1 — persister `last_regime.json` (le pont)

Le dict `regime_families` est **déjà calculé** (`daemon.py:1438`). Il suffit de le
sérialiser en fin de phase décision, atomiquement, estampillé.

```
# après daemon.py:1444 (regime_families calculé)
write_regime_atomic(STATE_DIR / "last_regime.json", {
    "asof": now.isoformat(),
    "families": regime_families,     # {famille: {dir, frac, up, down, n}}
})
```

- **Écriture atomique** (tmp + `os.replace`), même pattern que
  `write_universe_atomic`.
- **Fraîcheur (fast-fail, AX)** : le lecteur (`daemon.py:3128`) **rejette** un
  régime trop vieux (ex `asof` > 24 h → `market_context=None`) plutôt que d'injecter
  un régime périmé (piège week-end / marché fermé, cf incident Taïwan §CLAUDE.md).
- **Débloque** : (a) l'override de sélection D13-B reçoit enfin le régime ;
  (b) la Brique 2 a son signal côté rotation.

⚠️ **Fact-check Codex (finding #3)** : `build_market_context_from_regime`
(`wiring.py:281`) attend **directement** le dict `family_bias` (famille→{dir,frac,…}),
PAS un wrapper. Le lecteur actuel (`daemon.py:2759-2764`) passe le JSON **entier** à
cette fonction. Donc si on écrit `{asof, families}`, il faut **modifier le lecteur**
pour déballer `payload["families"]` (+ vérifier `asof` pour la fraîcheur), sinon un
faux régime est injecté. Alternative : écrire le dict `regime_families` brut et porter
`asof` dans un fichier/clé annexe.

### 4.2 Brique 2 — cap adaptatif au régime (largeur pilotée)

Remplacer la constante `cap_m` passée à `apply_hysteresis` par un
`cap_effective` calculé **par venue** au moment du `venue_close`, où l'on dispose
déjà des attractivités (`scan_and_rank`) et — via Brique 1 — du régime.

```python
# fonction PURE, déterministe (backtestable, D9 lock n°1)
def adaptive_cap(
    *,
    base: int,                 # radar.yaml cap_m (25) = point de référence
    ranked: list[dict],        # attractivités de la venue (scan_and_rank)
    regime_families: dict,     # familles présentes dans la venue (last_regime)
    venue_families: set[str],  # familles de cette venue
    p: AdaptiveCapParams,
) -> int:
    # signal régime : nb de familles de la venue en tendance cohérente
    n_regime = sum(
        1 for fam in venue_families
        if (regime_families.get(fam, {}).get("frac") or 0.0) >= p.regime_frac_min
    )
    # signal radar : nb de symboles au-dessus du seuil d'attractivité
    n_radar = sum(1 for it in ranked if abs(it["attractiveness"]) >= p.attract_min)

    # largeur = base + contribution des deux signaux (l'un OU l'autre ouvre)
    cap = base + p.k_regime * n_regime + p.k_radar * max(0, n_radar - base)
    # fusible seul : plancher bas, plafond = fusible de sécurité (pas une politique)
    return max(p.floor, min(cap, p.safety_cap))
```

- **Fusible seul** (décision #1) : `safety_cap ≈ 100` est un **garde anti-runaway**,
  pas un plafond métier. `floor ≈ 8` garantit qu'on surveille toujours le top.
- **Signal combiné** (décision #2) : `n_regime` (direction lisible) **et** `n_radar`
  (intensité brute) ouvrent la largeur ; l'un suffit.
- **Pas de churn** : le cap adaptatif pilote les **entrées** (combien de slots on
  *ouvre*). `apply_hysteresis` continue de gouverner les **sorties** par `dwell`/
  emergency. Un cap qui rétrécit **n'éjecte jamais** un incumbent sous dwell —
  invariant testé (§6). Concrètement : `cap_effective` remplace `cap_per_venue`
  dans l'appel `apply_hysteresis` (`venues.py:375`) ; la mécanique interne est
  inchangée.
- **Coût queue post-free-iteration** : pas de cap d'appels. `SAFETY_CAP` ne pilote
  pas l'admission queue ; le coût vivant est borné par timeout + AIMD + async +
  univers borné, avec `model_calls_used` comme métrique.

Paramètres nouveaux dans `radar.yaml` (calibrables, cf §7) :
```yaml
adaptive_cap:
  floor: 8
  safety_cap: 100
  regime_frac_min: 0.70     # réutilise le seuil D7 strong_families
  attract_min: 0.15         # seuil d'attractivité "opportunité" (à calibrer)
  k_regime: 4               # slots ouverts par famille en régime
  k_radar: 1                # slots ouverts par symbole très attractif au-delà de base
```

### 4.3 Brique 3 — analyse individuelle enrichie (profondeur)

Le grain-symbole existe (Phase 3, `decide_one.py`) mais dégradé. On le **dé-dégrade
et l'approfondit**.

- **Dé-dégrader** : `decide_handler` appelle `decide_batch(allow_context_request=
  True, allow_tool_calls=True)` + `recall_learnings` ON par symbole. Parité de
  capacités avec le batch, mais **isolée** (1 symbole = 1 contexte propre).
- **Approfondir** (décision #3, « plus profond que le batch ») :
  - **Budget par symbole** : un budget tokens/tours dédié (`decision_timeout_s`
    existe déjà par tâche, `planner_batch.py:311` ; ajouter un plafond de tours).
  - **Multi-tour conditionnel** : sur signal fort (trigger + régime aligné, ou
    demande explicite du LLM via `context_request`), autoriser un 2ᵉ/3ᵉ tour
    d'outils au lieu d'un unique pass. Le batch ne peut pas se le permettre (coût ×
    N symboles) ; l'individuel oui.
  - **Prompt focalisé** : analyse mono-symbole avec tout son contexte (indicateurs,
    news, watches, régime de *sa* famille, learnings recall pertinents) au lieu
    d'une vue comparative diluée sur 5.
- **La comparaison n'est pas perdue** : le `shared_context` (régime D2 + cockpit)
  reste injecté dans chaque appel (§3.5). La comparaison cross-asset est portée par
  le régime structuré, pas par le regroupement de prompt.
- **Réutilise l'infra Phase 3** : file durable, backpressure AIMD (`M`), isolation,
  crash-safe, `partition_key=symbole` — tout est déjà là (`decide_pool.py`,
  `queue_dispatch.py`). Cette brique **enrichit le handler**, elle ne réécrit pas
  l'orchestration.

### 4.4 Le flux de bout en bout (comment les 3 s'articulent)

```
CLÔTURE VENUE (EOD)                          CYCLE (runtime, ~15m)
──────────────────                           ─────────────────────
scan_and_rank (radar) ─┐                     analyzable_venues → univers actif
last_regime.json ──────┤                       (largeur = cap adaptatif, déjà écrite)
  (Brique 1)           ▼                      gate D7 (pertinence/cycle, INCHANGÉ)
cap_effective = f(régime, attractivité)  ──►    → decidable
  (Brique 2)                                  analyse individuelle enrichie /symbole
apply_hysteresis(cap_effective)                 (tools+recall+context+budget+multi-tour)
  → hotlist par venue                            (Brique 3)
override LLM (régime dans le prompt)          ← Decisions
  (débloqué par Brique 1)                     phase décision : calcule regime_families
                                                → sérialise last_regime.json ─┐
        ▲                                                                     │
        └──────────────── last_regime.json ◄─────────────────────────────────┘
```

Trois étages de sélectivité, du grossier au fin :
1. **Cap adaptatif** — *combien* de symboles on surveille (largeur, suit l'opportunité).
2. **Gate D7** — parmi eux, *lesquels* méritent un appel LLM ce cycle (pertinence).
3. **Analyse individuelle** — pour chacun retenu, *une analyse profonde*.

Le coût est ainsi **piloté par le signal à trois niveaux**, pas raboté : univers
large en opportunité, appels ciblés par pertinence, profondeur là où ça compte.

## 5. Composants

**Nouveaux :**
- `write_regime_atomic()` + garde de fraîcheur au lecteur (Brique 1) —
  `daemon.py:~1444` (écriture) / `daemon.py:3128` (lecture durcie).
- `adaptive_cap()` fonction pure + `AdaptiveCapParams` (Brique 2) — nouveau module
  `trader/market/rotation/adaptive_cap.py` ; `radar_config.py` étendu (parse
  `adaptive_cap:` de `radar.yaml`).

**Modifiés :**
- `venues.py:375` (Brique 2) : `cap_effective = adaptive_cap(...)` avant
  `apply_hysteresis` ; `run_venue_close`/`update_venue_ranking` reçoivent
  `regime_families` (chargé dans `tick`, déjà à disposition via Brique 1).
- `decide_one.py` / `decide_handler` (Brique 3) : flags `allow_*` ON, recall ON,
  budget/tours, multi-tour conditionnel.
- `daemon.py:2720` : le cap reste batch legacy ; ne pas le présenter comme borne queue.

**Inchangés (surface de risque bornée) :** `apply_hysteresis` (logique interne),
`relevance_gate` (D7), `order_admission`, recording, scheduler, la file Phase 3, le
format `decisions.jsonl`.

## 6. Invariants (tests TDD)

**Brique 1 :**
1. Après la phase décision, `last_regime.json` existe et contient
   `{asof, families}` avec le dict de `compute_family_bias`.
2. Lecteur : `asof` frais → `market_context` peuplé ; `asof` périmé (>24 h) ou
   fichier absent → `market_context=None` (fail-safe, pas de régime périmé).
3. Écriture atomique : un crash pendant l'écriture ne laisse pas de JSON tronqué.

**Brique 2 :**
4. `adaptive_cap` déterministe : mêmes entrées → même sortie (backtestable).
5. Régime fort **ou** radar riche → `cap_effective > base` ; marché haché (ni
   régime ni attractivité) → `cap_effective` décroît vers `floor`.
6. `floor ≤ cap_effective ≤ safety_cap` **toujours**.
7. **Pas d'éviction sous dwell** : un incumbent avec `dwell < dwell_days` reste
   sélectionné même si `cap_effective` rétrécit (le cap pilote les entrées, pas
   les sorties). Test d'invariant anti-régression.
   ⚠️ **Fact-check Codex : NON acquis par le code actuel.** `apply_hysteresis`
   tronque les incumbents à `cap_m` **avant** la logique dwell (`core.py:47-52`) ;
   la protection dwell ne joue qu'ensuite, au swap. Un cap qui rétrécit **peut**
   donc éjecter un incumbent sous dwell. Cette protection est à **ajouter**
   explicitement, pas à supposer (cf §10, finding #5).
8. `cap_effective` absent de régime (last_regime manquant) → retombe sur `base`
   (défaut D9 préservé, jamais 0).

**Brique 3 :**
9. Mode enrichi : `context_request`/tools/recall **actifs** par symbole (opposé de
   l'état dégradé actuel).
10. Isolation : un symbole qui échoue/timeout ne bloque pas les autres (déjà
    garanti par la file — test de non-régression).
11. Budget/tours : un symbole ne dépasse pas son plafond de tours ; multi-tour ne
    se déclenche que sur la condition « signal fort » définie.

## 7. Risques & points ouverts

- **Numéro de décision** : D15 en conflit possible avec un « D15 exploration »
  officieux (mémoire) alors que le registre s'arrête à D14. **Trancher avant** de
  toucher `registre-decisions-metier.md`.
- **Calibration** (tous à mesurer, pas à deviner) : `attract_min`, `k_regime`,
  `k_radar`, `floor`, `safety_cap`, seuil « signal fort » du multi-tour, plafond de
  tours. Défauts du §4.2 = points de départ, pas des vérités.
- **Coût réel en risk-on large** : « pas de limite si opportunité » + analyse
  enrichie + multi-tour peut faire beaucoup de tokens quand tout s'aligne. Assumé
  par Erwan, mais **mesurer** que le trio (cap adaptatif × gate D7 × backpressure
  AIMD) contient effectivement le coût et que le fusible 100 ne saute jamais en
  fonctionnement normal.
- **Perte de la vue comparative** : argumentée redondante (§3.5, régime + cockpit
  la portent). À valider en **A/B** — si le delta de qualité batch-vs-individuel
  penche côté batch, réintroduire une comparaison légère (ex top-3 pairs de la
  famille dans le prompt individuel).
- **`max_quiet_hours = 4.0` hardcodé** (`relevance_gate.py:28`) : hors périmètre
  mais lié — un univers plus large sous cap adaptatif rend ce défaut plus sensible
  (plus de `periodic_review`). À rendre configurable dans une itération suivante.
- **Cap par venue vs opportunité cross-venue** : `adaptive_cap` est par venue
  (cohérent D10). Une opportunité qui traverse les venues (thème global) n'est pas
  agrégée. Acceptable en v1 ; réévaluer si un thème cross-venue est raté.

## 8. Séquencement (livraison incrémentale, chaque étape réversible)

- **Étape 0 — Brique 1 (`last_regime`).** Petite, TDD, débloque les deux autres.
  Bénéfice immédiat isolé : l'override de sélection D13-B cesse d'être aveugle.
  *Livrable indépendant, mesurable via `rotation_ledger` (alpha override).*
- **Étape 1 — Brique 2 (cap adaptatif).** Dépend de l'Étape 0. Fonction pure d'abord
  (tests), puis câblage `venues.py`. Derrière un flag/param pour comparer
  cap-fixe vs cap-adaptatif. *Mesuré via `rotation_bench` (dynamique vs statique).*
- **Étape 2 — Brique 3 (analyse individuelle enrichie).** Indépendante des deux
  autres ; enrichit `decide_one` derrière le flag queue existant. *Mesuré via A/B
  qualité batch-vs-individuel sur même univers (le ticket ouvert de la spec phase3
  §7 se formalise ici).*

## 9. Mesure (comment on sait que ça marche)

| Brique | Instrument | Question tranchée |
|---|---|---|
| 1 | `rotation_ledger` (default vs final) | L'override enrichi par le régime bat-il le défaut déterministe ? |
| 2 | `rotation_bench` (dyn/statique/top-liq) | Le cap adaptatif capte-t-il l'opportunité sans gonfler le bruit ? |
| 3 | A/B batch vs individuel (même univers) | L'analyse individuelle enrichie améliore-t-elle la qualité de décision ? |
| tous | Coût/cycle (appels LLM, tokens) | Le trio cap×gate×AIMD contient-il le coût malgré l'analyse riche ? |

Aucune brique n'est « crue sur design » : chacune a son juge quantitatif avant
d'être promue défaut.

## 10. Fact-check Codex (2026-07-05)

Passe adversariale en lecture seule (session `factcheck-regime-adaptive-spec`).
Chaque prémisse tracée au code réel. Verdict global : **spec fondée, mais deux
prémisses cassent la conception si laissées telles quelles.**

### Prémisses factuelles
| # | Verdict | Preuve / note |
|---|---|---|
| 1 | VRAI | `regime_families` calculé `daemon.py:1256`, passé queue `:1457` + batch `:1472`. Nuance : sur l'univers **actif/tradable**, pas tout le pool radar. |
| 2 | VRAI | Lu `daemon.py:2759-2764` ; **0 écriture** de `last_regime` → `market_context=None`. |
| 3 | **NUANCE** | `build_market_context_from_regime` (`wiring.py:281-294`) attend `family_bias` **nu**, pas `{asof,families}`. Brique 1 doit déballer `families` sinon faux régime. → corrigé §4.1. |
| 4 | VRAI | `cap_m` → `apply_hysteresis` confirmé (`radar_config.py:13` → `venues.py:261,375`). FX = `fx_cap`. |
| 5 | **FAUX** | `apply_hysteresis` tronque les incumbents à `cap_m` **avant** dwell (`core.py:47-52`) → cap rétréci peut éjecter sous dwell. Invariant Brique 2 à **coder**, pas acquis. → corrigé §6. |
| 6 | VRAI | `decide_one.py:78-87` : `decide_batch(allow_context_request=False, allow_tool_calls=False)`, recall désactivé (`:13-17`). |
| 7 | VRAI | `batch_size=5` (`planner_batch.py:20`). |
| 8 | NUANCE | Gate D7 conditions OK (`relevance_gate.py:35-46`, seuil 0.70 `infra_holds.py:58`), mais `periodic_review` passe aussi si `hours_since_last_llm is None` (premier passage), pas que ≥4h. |
| 9 | **NUANCE** | Le `cockpit` n'est **pas** « barres+indicateurs » (`context.py:122` "no raw bars") et est bâti sur `tradable_symbols` (pas tout l'univers si stale). L'argument §3.5 tient **partiellement** ; ne prouve pas l'absence de perte comparative. |
| 10 | VRAI | `attractiveness` dispo (`radar.py:129-134` → `venues.py:396-407`). |
| 11 | VRAI batch legacy | `max_model_calls_per_cycle=25` (`daemon.py:984`, CLI `:2456`) ; queue/free-iteration = no call cap. |

### Défis de conception
- **A — Timing (risque réel).** `tick()` lit `last_regime` **avant** `run_cycle` dans
  la boucle (`daemon.py:2743-2773`) alors que le régime y est calculé (`:1256`). Au
  `venue_close` on a au mieux le régime du **cycle précédent**. Garde `>24h`
  insuffisant → estamper `asof`, `symbols`, `venues`, `daily_asof` ; viser un
  **régime par venue** (ou recalculé depuis les daily bars du radar).
- **B — Régime absent.** Robuste tant que cap fixe (fallback `params.cap_m`). Mais
  `adaptive_cap` doit **explicitement** retourner `base` si régime absent/malformed.
- **C — Coût cap large × D7 (pas solide tel quel).** `periodic_review` déclenche au
  1ᵉʳ passage (`_LAST_LLM_AT` volatile, reset au restart, `daemon.py:162-164`) → un
  cap large gonfle mécaniquement les « first seen » puis les revues 4h. Le fusible
  borne la casse mais ne **garantit pas** un coût contenu. À modéliser.
- **D — Perte comparative (risque qualité réel).** La spec Phase 3 dit « vigilance en
  review », pas « redondant prouvé » (`2026-07-04…:127`). L'A/B reste obligatoire.
- **E — Failles additionnelles à traiter :**
  1. **Signal circulaire (important).** Le régime sérialisé est calculé sur l'univers
     **déjà capé** → l'utiliser pour élargir le cap est circulaire. Le signal
     d'opportunité (régime **et** radar) doit être mesuré sur le **pool/venue
     complet**, pas sur l'univers capé.
  2. **Sémantique du coût.** `max_model_calls` compte des **chunks** en batch
     (`planner_batch.py:277-281`) ; la queue free-iteration n'a pas de cap d'appels.
     Ne pas réintroduire de limite d'admission par appels en queue.
  3. **Override pré-open.** Garde `free_slots=params.cap_m` (`venues.py:326`) → un
     `cap_effective > 25` rend les ajouts override incohérents (`core.py:155-156`).
     À aligner sur `cap_effective`.

### Top 3 à traiter avant de coder
1. **Coder** la protection dwell sous cap rétréci (finding #5) — sinon churn/éviction prématurée.
2. **Définir** fraîcheur + périmètre du régime : déballage `families`, `asof`, et
   calcul **par venue sur le pool non-capé** (findings #3, A, E1).
3. **Modéliser** le coût réel : cap large × `periodic_review` × sémantique fusible
   batch/queue (défis C, E2).
