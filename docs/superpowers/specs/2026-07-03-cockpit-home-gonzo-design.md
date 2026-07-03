# Cockpit — Home « mode Gonzo » (phase 1 TUI)

> **Type** : Spec (design validé).
> **Date** : 2026-07-03 · **Validé par** : Erwan (session brainstorming).
> **Code concerné** : `trader/cockpit/` · `trader/ui/` · `trader/read_models/runtime_state`.
> **Référence externe** : [control-theory/gonzo](https://github.com/control-theory/gonzo) — déjà
> dans le workflow (`make logs`) ; sa touche `d` ouvre Dstl8.Lite (compagnon web local).

## 1. Contexte & problème

La fondation data du cockpit est saine : read model unique et tolérant
(`load_runtime_state`, `trader/read_models/runtime_state.py:419`), builders Rich purs
testés, palette tokenisée (2 thèmes), events classés en 9 `EventClass`
(`trader/cockpit/events.py:18`). Mais la couche vue est un **rapport statique en
6 pages**, constat sur captures live (daemon vivant) :

1. **Zéro interactivité** — tout est `Static` + tables Rich : pas de curseur, pas de
   Enter→détail, pas de filtre (hors toggles `c`/`f` des logs).
2. **Redondance** — phase daemon ×3 (barre de statut `app.py`, tuile Décisions,
   tuile « Flux live »), appels LLM ×3, cycle/source ×3, learnings pending ×4.
   La tuile « Flux live » de la home duplique la barre de statut au lieu de
   montrer le flux.
3. **Charts faibles** — home : `_mini_line_chart` (`trader/cockpit/overview.py:148`)
   rend des rangées de blocs pleins → pâté illisible pour une variation <1 % ;
   page 2 : plotext avec thème `"pro"` et couleur `cyan` hardcodés
   (`trader/ui/rich_panels.py:180-181`) qui jurent sur fond saumon ; barres
   allocation et contrib insérées sans style → noires (`overview.py:365,380` ;
   `_bar` retourne une chaîne nue, `overview.py:177`). Aucune vue temporelle
   de l'activité.
4. **Contenu clippé silencieusement** — home en `overflow: hidden` : table
   « Risque sorties » réduite à son titre, 28 sorties → 4 visibles, 33 veilles →
   1 visible. Barre de statut (~14 champs, `app.py:285-302`) : à 200 colonnes,
   **Mode DRY/LIVE, kill-switch et learnings sont tronqués** — le kill-switch est
   une info de sécurité.
5. **Densité inégale** — page 4 : 2 lignes utiles sur un écran vide ; page 5 :
   2 colonnes utilisées ; page 3 : table branchée sur `state["decisions"]` du
   dernier rapport de cycle (souvent 0 hors batch) alors que `recent_decisions`
   (tail `decisions.jsonl`, n=50) est chargé.
6. **Couches sédimentaires** — gen-1 CLI Live (`trader/ui/tui.py` + `build_view`),
   gen-2 builders pages, gen-3 tuiles home. Code mort : `AttentionStrip`
   jamais composée (`app.py:305`), `_build_trades_with_pnl` sans appelant
   (`app.py:617`) — aucun test ne les référence directement (seul le builder
   sous-jacent `_build_attention_line` est testé).
7. **Sémantique sous-exploitée** — signaux multi-horizon calculés pour l'agent
   (`trader/agent/context.py:195`) jamais affichés ; attribution par raison de
   sortie enterrée page 3 ; aucune horloge de sessions/venues.

## 2. Objectif & principes

Home = **salle de contrôle dense sur 1 écran**, grammaire Gonzo :

- **1 info = 1 endroit** — la barre de statut est la seule source pour
  phase/LLM/cycle/source ; les tuiles n'en répètent rien.
- **Hiérarchie par criticité** — ce qui peut coûter de l'argent (kill, mode,
  risque@stops, stale, plans armés) toujours visible, jamais tronqué.
- **Temporalité visible** — histogramme d'activité 60 min ; plus d'agrégat sans
  dimension temps.
- **Tout est navigable** — chaque liste est un `DataTable` focusable,
  Enter → détail.

## 3. Décisions validées

| Décision | Choix |
|---|---|
| Ampleur | **A — Home Gonzo d'abord** : refonte home + interactions ; pages 2-6 conservées puis absorbées progressivement |
| Compagnon web « distl8 » | **Hors scope phase 1** ; phase 2 après le TUI (forme à re-trancher alors) |
| Thème | **Sombre par défaut** (polish gonzo-like) ; saumon FT conservé via `d` |

## 4. Layout cible

```
┌ ● VIVANT · 99 452$ ▂▃▅▄▆ · -0.55% (-548) · DRY · kill:nominal · LLM 2/25 · TW● EU 07:00 · 01:42Z ┐
│ ⚠ risque@stops -412$ · stale 4 · hard_stop 0%win · 2 armés exp<4h        (sinon : « RAS » vert)  │
├────────────────────────┬──────────────────────┬───────────────────────────────────────────────────┤
│ PORTEFEUILLE (2fr)     │ ACTIVITÉ 60min (1fr) │ DÉCISIONS — triage (1fr, DataTable)               │
│ équité braille palette │ exec   ▁█▂▁▁▂  n=2   │ 01:34 2303.TW BUY  exec   0.72                    │
│ top5 pos + risque@stop │ veille ▂▃▁▅▂▂  n=12  │ 01:34 9910.TW SELL exec   0.73                    │
│ contrib± / alloc       │ risk   ▁▁▁▁▁▁  n=0   │ 01:31 8046.TW HOLD veille 0.64  [Enter→détail]    │
├────────────────────────┴───────┬──────────────┴───────────────────────────────────────────────────┤
│ PLANS armés+sorties+veilles    │ FLUX LIVE (tail events colorisé — le vrai flux)                  │
│ (DataTable, tri risque/expiry) │ 01:34:35 SELL 9910.TW ✓ ordre 2000 @ 67.40                       │
└────────────────────────────────┴──────────────────────────────────────────────────────────────────┘
```

Taille cible : confortable à 200×50, fonctionnel dès ~160×40 (le statut et les
tuiles dégradent, cf. §5).

### 4.1 Barre de statut distillée (responsive)

Contenu : vital ● · équité + sparkline inline · P&L % (+USD) · mode DRY/LIVE ·
kill · LLM x/max · horloge venues (ouvertes + prochaine transition) · heure UTC ·
**progression de cycle conditionnelle** (`cycle 3/10` affiché uniquement quand un
batch tourne — rien quand le daemon est entre deux cycles).
**Ordre de rétrécissement défini** (terminal étroit) : on droppe d'abord
sparkline, puis horloge venues, puis P&L USD — **jamais** vital/mode/kill/équité.
Phase (chaîne brute), cycle ts et source quittent la barre (redondants ou peu
actionnables en continu ; restent visibles page 5 et dans le flux).

### 4.2 Ligne « à surveiller »

Anomalies uniquement, ordonnées par criticité (source : `attention_items`, §6) ;
si rien : « RAS » discret. Remplace l'`AttentionStrip` morte (supprimée).

### 4.3 Tuiles

- **Portefeuille (2fr)** : équité en braille via plotext **paramétré par la
  palette** (fenêtre glissante ~200 points, fallback sparkline existant) ; top 5
  positions **avec colonne risque@stop intégrée** (fusion de l'actuelle table
  « Risque sorties » clippée) ; contrib P&L ± et allocation en barres stylées
  palette.
- **Activité 60 min (1fr)** : une sparkline-ligne par état
  (exec / veille / plan / risk / stale / hold-quiet) + compteur `n=…`, buckets
  5 min, comme le mockup — pas de chart empilé. Source : `activity_buckets` (§6).
- **Décisions triage (1fr, DataTable)** : 8 dernières décisions priorisées
  (réutilise `_decision_priority`), colonnes UTC/Sym/Act/État/Conf/Suite
  (la source LLM part dans le modal détail).
- **Plans (DataTable unique)** : colonne « type » (armé / sortie / veille),
  ordre : armés d'abord, puis sorties par risque décroissant, puis veilles par
  expiration croissante.
- **Flux live** : tail `events.jsonl` colorisé par `EventClass` (le widget
  logs existant, réutilisé en tuile) ; remplace la tuile-tableau actuelle.
- La tuile « Observabilité » disparaît de la home (statut/attention la couvrent,
  détail en page 5).

## 5. Interactions

- **Tab / Shift+Tab** : focus entre panneaux (focus natif Textual, bordure
  accentuée). Les bindings pages `tab`→vue suivante actuels migrent vers
  `1..6` uniquement (Tab est repris pour le focus).
- **DataTable** : curseur ligne, tri.
- **Enter** → `SymbolDetailScreen` (ModalScreen) : position + plan de sortie +
  décisions récentes + learnings + santé data **du symbole** — pur filtrage du
  read model déjà en mémoire, zéro I/O nouvelle. `Esc` ferme.
- **Flux** : `f` pause (existant) · **`F`** modal filtre par `EventClass`
  (toggles, à la Ctrl+F Gonzo) · **`/`** filtre regex sur le texte des lignes.
- Raccourcis daemon inchangés : `s X k q d c l`.

## 6. Agrégats purs (nouveau module)

`trader/cockpit/aggregates.py` — fonctions pures, aucune I/O, TDD :

| Fonction | Contrat |
|---|---|
| `activity_buckets(recent_decisions, now, *, window_min=60, bucket_min=5)` | → séries par état (réutilise la logique de `_decision_status`) ; décisions sans ts ignorées |
| `risk_at_stops(holdings, trade_plans)` | → total USD au déclenchement des stops + pire position (généralise `_stop_risk_for_holding`) ; plans sans stop → comptés « sans stop », pas d'exception |
| `attention_items(state)` | → liste ordonnée (kill, halted, stale N, rejets risk N, armés expirant <1 h, positions sans stop) avec seuils explicites |
| `venue_clock(venue_state, open_venues_list, sessions, now)` | → venues ouvertes + prochaine transition (réutilise `trader.rotation.schedule`) |

Valeurs neutres sur données partielles (dict vide → agrégat vide, jamais de raise).

## 7. Architecture & nettoyage

- **Nouveau** : `trader/cockpit/aggregates.py` (purs) ·
  `trader/cockpit/home.py` (widgets + builders de la nouvelle home) ·
  `SymbolDetailScreen` (modal).
- **Modifié** : `app.py` (wiring home, bindings Tab/F//, statut responsive) ;
  thème `casys-ink` poli (défaut) ; `rich_panels.py` : plotext/barres prennent
  la palette (fix hardcodes).
- **Intouché** : builders gen-2 des pages 2-6 ; gen-1 `trader/ui/tui.py` (hors
  scope).
- **Supprimé** : `AttentionStrip` (app.py:305) et `_build_trades_with_pnl`
  (app.py:617) ; les tests de `_build_attention_line` migrent vers
  `attention_items` quand la ligne « à surveiller » la remplace.
- Erreurs : mêmes règles qu'aujourd'hui — lectures tolérantes, jamais de raise
  dans la boucle UI.

## 8. Invariants à tester (TDD)

1. `activity_buckets` : fenêtre vide → séries nulles ; décision hors fenêtre
   exclue ; bucket du bord inclus ; états mappés comme `_decision_status`.
2. `risk_at_stops` : LONG et SHORT ; fx_rate appliqué ; plan sans stop → listé
   « sans stop », total inchangé ; aucune position → zéros.
3. `attention_items` : kill actif → premier ; état nominal → liste vide ;
   ordre stable par criticité.
4. `venue_clock` : venue ouverte maintenant ; prochaine ouverture jour suivant ;
   état sessions absent → neutre.
5. Statut responsive : à largeur réduite, mode/kill/vital/équité toujours
   présents dans le rendu (test sur la chaîne rendue).
6. Home : plus aucune occurrence de phase/LLM/cycle/source dans les tuiles
   (anti-régression redondance).
7. Smoke `run_test` : home nouvelle, focus Tab, Enter ouvre le modal sur une
   ligne, Esc ferme, `F` et `/` filtrent le flux (pattern
   `tests/test_cockpit_smoke.py`).

## 9. Phasage (chaque étape shippable, review Codex pré-commit)

| Étape | Contenu |
|---|---|
| **A1** | `aggregates.py` + tests (aucun changement visuel) |
| **A2** | Statut distillé responsive + ligne « à surveiller » + dé-duplication des tuiles existantes |
| **A3** | Grille home : tuiles denses (portefeuille fusionné risque, histogramme activité, vrai flux) — encore `Static` |
| **A4** | DataTables + focus Tab + `SymbolDetailScreen` |
| **A5** | Filtres flux (`F`, `/`) + polish thème sombre par défaut |

Vérification visuelle à chaque étape : captures SVG headless
(`app.run_test` + `save_screenshot`, technique validée pendant l'analyse).

## 10. Hors scope phase 1

- Compagnon web « distl8 » (phase 2, forme à trancher alors : mini web-app SSE
  vs textual-serve).
- Suppression des pages 2-6 (absorption progressive, décidée plus tard).
- hjkl complet, command palette custom, skins supplémentaires.
- Tuiles multi-horizon (`agent/context.py` cp3) — candidat naturel de
  l'itération suivante, la donnée existe déjà côté agent.

## 11. Critères de réussite

1. La home répond en un coup d'œil à : *je perds ou je gagne ? quelque chose
   demande mon attention ? que fait le daemon là, maintenant ?* — sans changer
   de page ni scroller.
2. Kill-switch et mode visibles à toute largeur ≥120 colonnes.
3. Aucune donnée affichée en double sur la home.
4. Depuis n'importe quelle ligne (décision, position, plan) : Enter → contexte
   complet du symbole en <1 s.
5. Le flux est filtrable par classe et par regex sans quitter la home.
