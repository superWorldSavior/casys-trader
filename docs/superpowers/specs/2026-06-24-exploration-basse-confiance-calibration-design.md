# Exploration basse-confiance + boucle de calibration (paper)

Référence registre : candidate **D15** (à ouvrir après implémentation).

S'appuie sur D1 (missed ≠ bad), D6 (spirale auto-HOLD), D9 (concentration assumée),
D14 (sizing FX en natif, reject-not-clamp). Source du diagnostic : incident
2026-06-24 « plus aucune veille intelligente ».

Statut : **LIVRÉ** 2026-06-24 — commit 9905d51 (confiance non-bloquante + stop optionnel + calibration dé-censurée) ; mesure en cours (watches/basse-confiance).

---

## 1. Problème

Le système ne peut **structurellement pas calibrer sa confiance dans la zone
basse** (< 0.7). Quatre mécanismes empilés verrouillent cette zone :

1. **Gate de confiance** (`trader/risk.py`, `RiskGate.check_confidence`) : rejette
   tout ordre dont `confidence < required`, où
   `required = min_trade_confidence + (full_risk_confidence − min_trade_confidence) × clamp(risk_pct/max, 0, 1)`
   — plancher `0.7`, jusqu'à `0.9` au budget plein (`config/risk.yaml:15-16`).
2. **Prose du mandat** (`mandate/mandate.md`) : « si confiance 0.5-0.7, reste HOLD
   ou attends … au lieu de gaspiller un cycle sur un ordre voué au rejet ». Un
   modèle faible (spark/ollama) la **sur-applique** : il confond « ne trade pas »
   et « ne fais rien » → n'arme même plus de veille.
3. **Censure de l'attribution** (`trader/attribution.py`, `_filter_regime_trips`,
   ~lignes 287-322) : tout trip dont `entry_confidence < min_entry_confidence` est
   **jeté** des stats de calibration.
4. **Spirale learnings** (D6) : les learnings d'abstention se réinjectent et
   renforcent le HOLD.

**Conséquence** : l'agent ne reçoit jamais de feedback P&L sur ses calls < 0.7 → il
ne peut pas apprendre s'il est sous-confiant (de bons calls bridés) ou sur-confiant.
La zone basse est un angle mort permanent.

### Preuve (diagnostic 2026-06-24)

- Les veilles (`indicator_watch`) sont tombées à **0/jour** depuis le merge FX
  (comparaison contrôlée : même modèle ollama, watches → 0 au seul changement de
  code). Suppression multi-causale confirmée (sizing-guidance FX + prose confiance
  + learnings).
- A/B offline : en retirant la prose confiance + le bloc sizing, le spark réarme
  des veilles **à confiance 0.62-0.66** (pullback `z_score ≤ 0.5`) — exactement les
  bonnes veilles perdues.

---

## 2. Décisions de design (validées Erwan, 2026-06-24)

- **Objectif = capter l'edge AUSSI**, pas seulement calibrer : vraies positions
  sizées par le jugement de l'agent.
- **La confiance est DÉCOUPLÉE de la taille.** Elle ne gate plus rien ; c'est une
  **prédiction pure** que l'agent déclare et qu'on mesure ex-post. Découpler évite
  que l'agent gonfle sa confiance pour débloquer du size → signal de calibration
  **non-gameable**.
- **Pas de floor.** L'agent met la taille qu'il veut, bornée uniquement par les
  fusibles durs.
- **Garde-fous : l'agent gère.** Aucun nouveau mécanisme dur (pas de cap
  d'exposition exploration dédié, pas de plancher notionnel fee-aware). L'agent
  voit déjà `be_ref_bps`, le gross et le régime ; il est responsable des frais et
  de la concentration. Seuls les fusibles existants subsistent.
- **Stop NON obligatoire.** L'agent met un `hard_stop` s'il veut (et choisit son
  niveau, D11), mais rien ne l'y oblige. On NE rend PAS le stop obligatoire.
  Conséquence assumée : un trade sans stop n'a aucun plafond de risque-au-stop ; il
  est borné uniquement par les **fusibles notionnels** (`max_position_value`,
  `max_order_value`, `max_gross_exposure`). `max_risk_per_trade` (1 %) ne s'applique
  que lorsqu'un stop existe (risque calculable). À l'agent de gérer le drawdown
  d'une position sans stop (veille / revue).
- **`max_position_value` 20 000 $ → 30 000 $** (~30 % de l'equity courante ;
  concentration assumée, cohérent D9). Dollar fixe (style config actuel) ; passage
  en %-de-l'equity dynamique reporté (YAGNI).
- **Dé-censure de l'attribution** : la zone basse entre dans la calibration.
- **Paper-only** : ces réglages relâchés ne doivent jamais être hérités par un
  profil live.

---

## 3. Architecture

Changement **paramétrique + prose**, pas de nouveau module. Frontières touchées,
toutes existantes.

**Modèle de risque après changement.** En retirant le gate confiance, on retire
aussi la borne « risque non-borné → `full_risk_confidence` exigée » (`risk.py:145-148`)
qui forçait indirectement un stop. On NE la remplace PAS par un stop obligatoire
(décision Erwan). Le risque de chaque trade est donc borné par :

- **avec stop** : `max_risk_per_trade` (1 %) au stop **+** fusibles notionnels ;
- **sans stop** : fusibles notionnels **seuls** (`max_position_value` 30k /
  `max_order_value` 10k / `max_gross_exposure` 100k).

Aucun trade n'est jamais « non-borné » au sens absolu (le notionnel plafonne
toujours), mais une position sans stop peut subir un mouvement adverse sans sortie
automatique — géré par l'agent.

---

## 4. Les frontières touchées

### 4.1 Gate de confiance — désactivation explicite (`trader/risk.py`, `config/risk.yaml`)

Ajouter un flag **explicite** (AX #7) plutôt que de neutraliser par valeurs magiques :

```yaml
# config/risk.yaml
confidence_gate_enabled: false   # paper : la confiance ne gate plus la taille
```

`RiskGate.check_confidence` court-circuite quand `confidence_gate_enabled is False`
→ verdict toujours OK, `confidence` enregistrée mais jamais comparée. Les seuils
`min_trade_confidence` / `full_risk_confidence` sont **conservés en config**
(réf. / ré-activation live), mais inertes tant que le flag est `false`.

Invariant : avec le flag à `false`, AUCUN ordre n'est jamais rejeté pour
`risk:confidence_below_required`, quelle que soit la taille — **y compris un ordre
sans stop** (le chemin « risque non-borné → 0.9 » de `risk.py:145-148` ne s'applique
plus). Un trade sans stop passe le gate et reste borné par les fusibles notionnels.

### 4.2 Plafond de position (`config/risk.yaml`)

`max_position_value: 20000 → 30000`. Aucun changement de code (`RiskGate` lit déjà
la valeur). Les autres fusibles **inchangés** : `max_risk_per_trade_pct: 0.01`,
`max_gross_exposure: 100000` (no-leverage), `max_order_value: 10000`,
`max_orders_per_cycle: 5`, `min_equity: 50000`.

### 4.3 Dé-censure de l'attribution (`trader/attribution.py`, câblage `trader/daemon.py`)

**Les buckets de calibration existent DÉJÀ** : `compute_attribution` produit
`by_confidence` (tranches type `0.85-1.0` avec n/win_rate). Le seul verrou est le
filtre `_filter_regime_trips` qui jette les trips sous `min_entry_confidence`.

Fix (implémenté) : helper pur `daemon._attribution_min_entry_confidence(risk_cfg,
confidence_gate_enabled=…)` → retourne **None** quand le gate est off (aucune
censure : les trips basse-confiance entrent dans les buckets `by_confidence`),
sinon `min_trade_confidence`. Câblé au site `compute_attribution(...)` du daemon.
(`_filter_regime_trips` conserve déjà tout trip dont `entry_confidence` est None.)

Follow-up non bloquant : distinguer les trips d'exploration récents des anomalies
historiques pré-gate (tag par `code_version`/date) si la calibration forward se
trouve polluée — à mesurer, pas à faire d'avance.

### 4.4 Réécriture de la prose mandat (`mandate/mandate.md`)

Le bloc confiance actuel (stashé `stash@{0}`, à **dropper** — superseded) est
remplacé. Nouveau message (esprit, formulation finale en implémentation) :

- La confiance **pilote la calibration, pas l'action**. Elle ne bloque rien.
- Confiance moyenne/basse n'est PAS un ordre de rester inerte : explore **en
  petit** (la taille est ton choix, bornée par les fusibles) ou **arme une
  `indicator_watch`**. L'inaction n'est justifiée que s'il n'y a rien à surveiller.
- Déclare ta confiance **honnêtement** : l'attribution te renvoie, par bucket, si
  tes calls basse-confiance gagnent — c'est ta boucle de calibration.
- Tu restes responsable des **frais** (`be_ref_bps`) et du **gross** ; aucun
  garde-fou code ne le fait à ta place.

Invariant prose : plus aucune formulation « sous X, reste HOLD / n'essaie pas ».

**`mandate/guardrails.json`** : la note « Pas d'ouverture sans stop » devient
incohérente (stop désormais optionnel). La reformuler en **recommandation** (« un
`hard_stop` est fortement recommandé ; sans stop, la position n'est bornée que par
les plafonds notionnels ») plutôt qu'en interdit — l'agent reste libre.

### 4.5 Feedback de gate dormant (`trader/daemon.py`, `_merge_gate_feedback`)

Avec le gate désactivé, la branche `reason == "risk:confidence_below_required"`
devient morte. La laisser dormante (inoffensive) ; nettoyage optionnel.

### 4.6 Stop optionnel — guardrail `missing_hard_stop` (`trader/daemon.py`, `config/risk.yaml`)

Découverte à l'implémentation : le rejet « ouverture sans hard_stop » N'EST PAS
porté par `risk.py` mais par un guardrail **dur** dans `run_cycle`
(`reason="risk:missing_hard_stop"`, ~`daemon.py:2302-2320`), avec test dédié. Pour
rendre le stop optionnel, ajout d'un flag explicite **`require_hard_stop`**
(`config/risk.yaml`, défaut **True** = guardrail D6 préservé, live-safe). Le rejet
devient `if pure_open and require_hard_stop:`. Avec `require_hard_stop=false`, une
ouverture sans stop passe ; sa `quantity` n'est PAS clampée au risque (pas de stop)
et reste bornée par les fusibles notionnels via `gate.check`.

---

## 5. Invariants / tests (test-first, AX #11)

1. `confidence_gate_enabled: false` → `check_confidence` retourne toujours OK, même
   à `confidence=0.01` au budget de risque plein. Aucun rejet
   `risk:confidence_below_required`.
2. `confidence_gate_enabled: true` (défaut historique) → comportement actuel
   **inchangé** (non-régression : la formule et les rejets fonctionnent).
3. Les fusibles durs rejettent toujours indépendamment de la confiance :
   `max_position_value` (30 k$), `max_order_value`, `max_gross_exposure`, et
   `max_risk_per_trade` (1 %) **quand un stop est présent**.
3bis. **Stop optionnel** : un ordre d'ouverture SANS `hard_stop` n'est PAS rejeté ;
   il passe et reste borné par les seuls fusibles notionnels. `max_risk_per_trade`
   ne s'applique pas (pas de distance au stop calculable).
4. `max_position_value` accepte 30 000 ; une position visant 25 k$ passe, 31 k$ est
   rejetée.
5. Attribution : avec `min_entry_confidence=None`, un trip à `entry_confidence=0.55`
   est **conservé** et apparaît dans son bucket de calibration.
6. Calibration par bucket : la sortie expose n/win%/P&L par tranche de confiance.
7. Mandat : un test de présence/absence garantit qu'aucune formule « reste HOLD
   sous seuil » ne subsiste, et que la consigne « explore en petit / arme une
   veille » est présente.

---

## 6. Hors scope (YAGNI) & gardes

- **Garde paper-only** : `confidence_gate_enabled: false` et
  `max_position_value: 30000` vivent dans la config **paper**. Aucun profil live
  n'existe aujourd'hui ; le jour où il existe, il DOIT fournir sa propre `risk.yaml`
  (gate réactivé, plafonds prudents). Documenté ici comme exigence ; pas de
  machinerie de profils construite maintenant.
- **Pas de cap d'exposition exploration ni de plancher notionnel fee-aware** :
  décision explicite « l'agent gère ».
- **Sizing %-de-l'equity dynamique** pour `max_position_value` : reporté (dollar
  fixe pour l'instant).
- **Suppression résiduelle de veilles** : si, après déploiement, les
  `indicator_watch` ne repartent pas malgré (4.1)+(4.4), traiter en follow-up le
  bloc sizing-guidance FX (`codex_client._DECISION_GUIDANCE`) et la spirale
  learnings D6 (`missed_moves` contrefactuels, D6 étape 3). **À mesurer**, pas à
  graver d'avance.

---

## 7. Mesure (post-déploiement)

- Reprise des `indicator_watch` armées/jour (réf. avant incident : ~60-220/j EU).
- Volume de trades basse-confiance (< 0.7) et leur P&L net par bucket.
- Calibration : écart entre confiance déclarée et win% réalisé par bucket, suivi
  dans le temps (l'agent se calibre-t-il ?).
- Frais : part des trades dont le mouvement réalisé < `be_ref_bps` (l'agent
  gère-t-il bien la contrainte frais sans garde-fou code ?).
