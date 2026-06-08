# Consolidateur de learnings — « boucle 1.5 »

**Date** : 2026-06-08
**Statut** : design validé, **implémenté** (cf. § Timing)
**Auteurs** : Erwan + Claude

---

## 1. Contexte

Le runtime de `casys-trader` a une boucle de feedback (décidée le 2026-06-06,
commit `09a810d`) :

- **Boucle 2 (machine, par réveil)** : l'agent décideur émet un champ `learning`
  à chaque cycle ; le daemon le persiste dans `state/learnings.jsonl`
  (machine-owned, borné) AVEC le résultat de la décision, et réinjecte les N
  derniers via `context.learnings` au réveil suivant (`daemon.py:696`).
- **Boucle 1 (humaine, périodique)** : Erwan + Claude distillent ce flux vers
  `mandate/memory.md` (interface humaine, gardée séparée du flux machine).

Ce design insère une **boucle intermédiaire, machine** entre les deux.

## 2. Problème observé

Inspection de `state/` le 2026-06-08 (~23 h de run, 533 cycles) :

- `learnings.jsonl` contient **3 entrées**, toutes `BTC-USD`, toutes sur une
  fenêtre d'1 h (03:06 → 04:06 UTC), et **quasi identiques** — la même thèse
  reformulée trois fois :
  > « z élevé mais ER bas + données vieillies → privilégier HOLD, attendre
  > confirmation de régime ».
- La réinjection (`daemon.py:696`, `learnings_store.recent(limit=...)`) renvoie
  **les N derniers, tous symboles confondus, sans aucun filtre**. Un agent
  décidant sur `SPY` reçoit donc trois learnings qui ne parlent que de `BTC`.

Deux défauts distincts :

1. **Redondance** : la fenêtre réinjectée peut être saturée par une seule idée
   répétée → perte de diversité, pollution du contexte.
2. **Absence de portée** : aucun mécanisme ne distingue un apprentissage
   spécifique à un symbole d'un apprentissage transversal, ni ne route le bon
   apprentissage vers la bonne décision.

> **Mise en garde de proportionnalité.** Ces 3 learnings sont probablement le
> même artefact « stale-data » que les 208 décisions `stale_market_data`
> observées (90 % des cycles bloqués par la garde fraîcheur, 0 trade exécuté).
> Ce n'est PAS un signal de fond validé. Voir § Timing.

## 3. Idée directrice

Séparer les responsabilités (AX #8, Composable Primitives) :

- **L'agent décideur reste bête et rapide** : il crache une note brute, jetable,
  par réveil. Il ne se soucie ni des doublons ni de la portée.
- **Un agent consolidateur dédié** (Codex, via `acpx codex exec`, stateless) passe
  périodiquement, lit le pool brut, et écrit *les vrais apprentissages* —
  fusionnés, raffinés, et classés par portée.

Gain clé : la détection de « c'est la même idée » et la promotion au transversal
ne se font **plus en code** (difflib / embeddings, fragiles sur des
reformulations) mais **par un agent qui comprend le sens**.

Trois niveaux de mémoire, frontière machine/humain préservée :

```
learnings.jsonl            learnings_consolidated.json        mandate/memory.md
(brut, jetable)      →     (consolidé, machine, réinjecté)  →  (humain, boucle 1)
  agent décideur              consolidateur (boucle 1.5)         Erwan + Claude
```

## 4. Décisions de design

| Axe | Décision | Justification |
|-----|----------|---------------|
| Sens du doublon | **Récurrence = signal** (gardée), mais exprimée **qualitativement** | Un LLM ne maintient pas un compteur entier de façon fiable ; « confirmé ~10× » suffit |
| Cible d'écriture | **Nouveau store machine dédié** (`learnings_consolidated.json`) | `memory.md` reste 100 % humain ; trace brute préservée ; séparation nette |
| Déclencheur | **Seuil de N nouveaux bruts** (défaut 50) | Event-driven, déterministe (pas de dépendance au temps mural), colle au modèle daemon |
| Mode de travail | **Stateless : re-synthèse totale** | Idempotent, simple, cohérent avec la philo projet (batch C, sessions jetables) |
| Modèle de scope | **Binaire : global vs par-symbole** | Résout exactement le défaut observé ; routage trivial ; raffinable plus tard |

## 5. Architecture

### 5.1 Store brut `state/learnings.jsonl` *(existant, ajusté)*

Reste le buffer d'entrée. **Changement** : relever `LearningsStore.max_entries`
de 50 à **≫ N** (proposition : 200) pour qu'aucun brut ne soit tronqué avant
d'avoir été consolidé.

> Note de correction vs description initiale : `LearningsStore` n'est **pas** un
> journal permanent. C'est un **buffer glissant borné** (`memory.py:58`,
> `rows = rows[-self.max_entries:]`). D'où l'invariant `max_entries(brut) ≫ N`.

### 5.2 Nouveau store `state/learnings_consolidated.json` *(machine-owned)*

```json
{
  "watermark": "2026-06-07T04:06:46.958139+00:00",
  "global": [
    { "note": "Données globales vieillies → HOLD ; ne pas scalper sur des prix gelés.",
      "robustness": "confirmé sur plusieurs symboles" }
  ],
  "by_symbol": {
    "BTC-USD": [
      { "note": "z élevé sans ER/ac confirmés → HOLD, attendre confirmation de régime." }
    ]
  }
}
```

- `watermark` : `ts` du dernier brut inclus dans la dernière consolidation
  (sert au déclencheur, cf. 5.3).
- Bornes : **max 10 entrées `global`, max 5 par symbole**.
- Écriture **atomique** (temp + `os.replace`), sur le modèle de
  `LearningsStore.append` (`memory.py:60-64`).

### 5.3 Le consolidateur *(nouveau module `trader/consolidator.py`)*

Appelé à la fin de chaque cycle daemon. Algorithme :

1. Lire le store consolidé (→ `watermark`, `consolidated`).
2. Lire les bruts ; sélectionner ceux avec `ts > watermark` → `new_raw`.
3. **Si `len(new_raw) < N`** (défaut 50) → ne rien faire, retour silencieux.
4. Sinon → 1 appel Codex **stateless** (`acpx codex exec`) avec en entrée
   `(consolidated, new_raw)` ; réception du **consolidé entier régénéré**
   (structure 5.2).
5. Parser/valider la sortie (structure `global` / `by_symbol`, bornes).
6. Écrire atomiquement ; **avancer `watermark`** au `ts` max de `new_raw`.

Config dédiée, indépendante de l'agent décideur :

- seuil : `--learning-consolidation-threshold` /
  `TRADER_LEARNING_CONSOLIDATION_THRESHOLD`, défaut `50`.
- binaire ACPX : `--consolidator-acpx-bin` /
  `TRADER_CONSOLIDATOR_ACPX_BIN`, défaut `acpx`.
- agent ACPX : `--consolidator-acpx-agent` /
  `TRADER_CONSOLIDATOR_ACPX_AGENT`, défaut `codex`.
- modèle : `--consolidator-model` / `TRADER_CONSOLIDATOR_MODEL`, défaut
  `gpt-5.5[high]`.
- timeout : `--consolidator-timeout-s` / `TRADER_CONSOLIDATOR_TIMEOUT_S`,
  défaut `240`.

Fonctions composables (AX #8) : `select_new_raw(raw, watermark)`,
`consolidate_payload(consolidated, new_raw) -> dict | None` (l'appel modèle),
`ConsolidatedLearningsStore.write(result, watermark)`.

### 5.4 Contrat de l'agent consolidateur *(prompt, dans `codex_client`)*

L'agent reçoit le consolidé courant + les nouveaux bruts, et doit :

- **Dédupliquer / fusionner** les notes qui expriment la même thèse.
- **Promouvoir au `global`** ce qui traverse plusieurs symboles OU est
  intrinsèquement transversal (data-quality, régime de marché, discipline de
  sortie) ; garder en `by_symbol` ce qui est spécifique.
- Exprimer la **récurrence qualitativement** dans `robustness` (« confirmé ~10× »,
  « observé sur 3 symboles »), **jamais** un compteur numérique à maintenir.
- **Préserver verbatim les entrées stables** du consolidé courant, sauf si un
  brut nouveau les change matériellement (clause anti-drift, cf. § Tensions).
- Respecter les bornes (10 global, 5/symbole) : si dépassement, fusionner ou
  écarter les moins robustes.

### 5.5 Intégration daemon — réinjection scope-aware + cold-start

`daemon.py:696` passe d'une liste plate à une structure, avec **fallback** :

- **Consolidé non vide** → injecter `global` + `by_symbol[symbole]` **+** les 3
  derniers bruts (continuité immédiate, le temps que la conso rattrape).
- **Consolidé vide** (état actuel, < N bruts cumulés) → **comportement
  d'aujourd'hui préservé** : les bruts récents. **Zéro régression.**

> En batch stateless C (1 appel partagé pour tout l'univers), l'agent reçoit la
> structure complète (`global` + tout `by_symbol`) ; le « routage » est qu'il
> voit les leçons rangées par portée. Si le runtime repasse un jour en
> per-symbol, le routage `global + by_symbol[X]` devient direct.

## 6. Ce qui NE change pas

- L'agent décideur reste bête (crache une note brute).
- `mandate/memory.md` (interface humaine, boucle 1) **intouché**.
- `broker` / `risk` / `exit_engine` inchangés.
- Le consolidateur ne passe **aucun ordre marché** → safe à lancer même hors
  `dry_run` (lecture/écriture `state/` uniquement).

## 7. Hors scope (YAGNI, assumé)

- Scope **thématique** (tags data-quality/regime/risk) ou **par classe d'actif**
  (crypto/fx/equity/commodity).
- **Compteur numérique** de récurrence.
- **Embeddings** / similarité lexicale côté code.
- **Déclencheur temporel** (intervalle horaire).

Le binaire global/par-symbole + seuil N suffisent à résoudre le défaut observé.
Le reste se rajoute si le besoin se prouve.

## 8. Tensions assumées (documentées)

- **Récurrence « comptée » (choix initial) → qualitative en pratique.** Un LLM
  ne maintient pas un entier de façon fiable ; on garde le *signal* de robustesse,
  pas le compteur.
- **Re-synthèse totale → léger non-déterminisme résiduel** (frotte avec AX #6,
  Deterministic Outputs). Borné par la clause « préserve verbatim les entrées
  stables » du contrat (5.4).

## 9. Invariants testés (TDD)

1. `select_new_raw` ne retient que les bruts avec `ts > watermark`.
2. Le consolidateur ne se déclenche **pas** si `len(new_raw) < N`.
3. Il se déclenche **exactement** à `len(new_raw) >= N`.
4. **Cold-start** : consolidé vide → la réinjection retombe sur les bruts récents
   (pas de contexte learnings vide).
5. Après consolidation, `watermark` = `ts` max des bruts consommés.
6. Parsing de la sortie : structure `global` / `by_symbol` valide ; bornes
   (10 / 5) respectées ; sortie malformée → consolidé courant inchangé (fail-safe).
7. Écriture atomique : un crash en cours d'écriture ne corrompt pas le store.
8. `max_entries(brut) > N` (invariant de non-perte).
9. Réinjection scope-aware : symbole X reçoit `global` + `by_symbol[X]`, pas
   `by_symbol[Y]`.

## 10. Timing — implémentation

**Décision initiale (2026-06-08)** : écrire le spec maintenant (frais en tête),
et différer tant que le flux brut n'avait que quelques artefacts stale-data.

**Mise à jour (2026-06-08)** : le buffer brut contient désormais assez de
learnings multi-symboles pour valider la mécanique. Le consolidateur est
implémenté en fail-safe :

- `state/learnings.jsonl` reste le buffer brut borné.
- `state/learnings_consolidated.json` devient le store machine réinjecté.
- Cold-start : si le consolidé est vide, le contexte garde les bruts récents.
- Après consolidation : le contexte reçoit `global`, `by_symbol` et quelques
  bruts récents.
- Sortie LLM invalide ou échec fournisseur : le consolidé courant reste inchangé.

**Ce que cela ne prouve pas encore** : les learnings restent majoritairement des
HOLD sans fills ni P&L. La mécanique peut fonctionner; la doctrine de trading ne
doit pas être durcie tant qu'on n'a pas de résultats d'exécution.

## 11. Process

- Implémentation en **TDD** (`uv run pytest -q`), invariants du § 9 d'abord.
- **Review Codex pré-commit** via acpx (skill `pair-codex`) sur le diff.
