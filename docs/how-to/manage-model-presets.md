# How-to — Changer le preset de modèles

> **Type** : How-to (Diátaxis) — procédure opérateur.
> **Commande** : `scripts/model_preset.py` · **Presets** :
> `ops/model-presets/*.env` · **Cible** : `.env`.

Un preset configure ensemble les cinq rôles LLM : brain trader,
consolidateur, agent Univers, analyste micro et analyste macro/news. Le daemon
ne relit pas le `.env` à chaud : toute bascule réellement écrite doit être
suivie d'un redémarrage.

## 1. Inspecter l'état courant

Depuis la racine du dépôt :

```bash
uv run python scripts/model_preset.py list
make models
```

Pour une sortie exploitable par une commande :

```bash
uv run python scripts/model_preset.py --json show
```

`show` décrit le bloc résolu dans le `.env`. Il ne prouve pas encore quel
preset utilise un daemon déjà lancé.

## 2. Prévisualiser la bascule

Le dry-run est le comportement par défaut :

```bash
make model-preset PRESET=grok
```

Vérifier les valeurs ajoutées, modifiées et retirées. Le script retire aussi les
assignations gérées restées hors du bloc preset, car une occurrence placée plus
haut dans le `.env` gagnerait silencieusement au chargement.

## 3. Appliquer

```bash
make model-preset PRESET=grok WRITE=1
make models
```

L'écriture :

- réécrit le bloc `casys:model-preset` et retire les assignations des clés
  gérées qui subsistaient hors de ce bloc ;
- conserve toutes les autres lignes du `.env` ;
- conserve une copie précédente dans `.env.bak` ;
- ne modifie pas les fichiers versionnés de `ops/model-presets/`.

Ne pas éditer le bloc généré à la main. Pour revenir au preset précédent,
réappliquer son nom avec la même procédure. `.env.bak` peut contenir des secrets
et ne doit jamais être ajouté au dépôt.

## 4. Redémarrer le daemon

Effectuer un arrêt puis un lancement via le superviseur ; un simple lancement
pendant que l'ancien processus vit encore est refusé par l'anti-doublon. Suivre
[Lancer / relancer / arrêter le daemon](run-the-daemon.md#relancer-le-daemon).

## 5. Vérifier le runtime, pas seulement le `.env`

Après le redémarrage, attendre un **nouvel appel LLM réel**. Pour le brain :

```bash
jq -c 'select(.model_called == true) |
  {cycle_ts,symbol,llm_provider,llm_model,llm_fallback_reason}' \
  state/decisions.jsonl | tail -1
```

La ligne doit être postérieure au redémarrage. Le fichier lisible
`state/agent_trace.log` donne la même information :

```bash
rg 'model=' state/agent_trace.log | tail -5
```

Pour l'agent Univers, contrôler un run lui aussi postérieur au redémarrage :

```bash
jq '{as_of,venue,status,agent_provider,agent_model,
     agent_provider_fallback_reason}' \
  state/universe_runs/latest-TW.json
```

Une projection ancienne ne constitue pas une preuve du nouveau preset. Si
`llm_fallback_reason` (brain) ou `agent_provider_fallback_reason` (Univers) est
renseigné, le modèle affiché peut être celui du fallback : examiner alors les
logs avant de conclure que la bascule a échoué.

## En cas d'échec

- `preset_not_found` : relancer `scripts/model_preset.py list` et utiliser le
  nom exact ;
- `block_unterminated` : le script refuse volontairement d'écrire un `.env`
  ambigu ; comparer le bloc avec `.env.bak` avant toute réparation ;
- authentification ou profil rejeté au premier appel : contrôler le profil
  isolé correspondant, puis consulter la
  [référence des presets](../reference/model-presets.md) et la
  [référence du profil Codex](../reference/codex-home-isole.md).
