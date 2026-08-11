# Référence — presets de modèles LLM

> **Type** : Reference (Diátaxis).
> **Code** : `scripts/model_preset.py` · presets `ops/model-presets/*.env` · résolution `trader/agent/llm.py::build_default_router_from_env`.
> **Statut** : ✅ actif depuis le 2026-07-29. Preset courant : `codex-luna-medium`.
> **Rôle** : basculer d'une famille de modèles à l'autre en une commande, sans perdre les réglages de la famille qu'on quitte.

## Le problème

Cinq rôles LLM, chacun avec sa paire de variables. Tous les rôles Codex partagent
le même profil nu ; le brain peut en plus fixer son effort sur sa session ACP :

| Rôle | Agent | Modèle | Effort |
| --- | --- | --- | --- |
| Brain trader (décision) | `TRADER_ACPX_AGENT` | `TRADER_MODEL` | `TRADER_REASONING_EFFORT` |
| Consolidateur learnings | `TRADER_CONSOLIDATOR_ACPX_AGENT` | `TRADER_CONSOLIDATOR_MODEL` | profil commun (`low`) |
| Agent univers (rotation) | `TRADER_UNIVERSE_ACPX_AGENT` | `TRADER_UNIVERSE_MODEL` | profil commun (`low`) |
| Analyste micro société | `TRADER_COMPANY_MICRO_ACPX_AGENT` | `TRADER_COMPANY_MICRO_MODEL` | profil commun (`low`) |
| Analyste macro/news | `TRADER_NEWS_MACRO_ACPX_AGENT` | `TRADER_NEWS_MACRO_MODEL` | profil commun (`low`) |

Changer de famille à la main, c'est réécrire une dizaine de lignes sans en
oublier une — et l'ancienne configuration finit en lignes commentées qu'on perd
au commit suivant.

## Le mécanisme

Chaque famille est **un fichier versionné** dans `ops/model-presets/`. Le script
écrit le jeu retenu comme un **bloc délimité** du `.env` :

```
# >>> casys:model-preset=codex-luna-medium >>>
…
# <<< casys:model-preset <<<
```

```bash
make models                                   # jeu actif + preset courant
make model-preset PRESET=kimi                 # DRY-RUN : le diff, rien d'écrit
make model-preset PRESET=kimi WRITE=1         # écrit .env (+ .env.bak)
uv run python scripts/model_preset.py --json show   # sortie machine
```

`${REPO}` dans un preset est substitué par la racine absolue du dépôt à
l'écriture, afin que les profils isolés restent versionnables.

## Presets livrés

| Preset | Brain | Consolidateur / univers / micro / macro |
| --- | --- | --- |
| `codex-luna-medium` | `gpt-5.6-luna` **medium** | `gpt-5.6-sol` **low** |
| `kimi` | `kimi-code/kimi-for-coding` **high** | `kimi-code/k3-256k` **high** (micro : `kimi-for-coding`) |

Le preset Codex utilise un seul `ops/codex-home` low. Pour le brain seulement,
le runtime appelle le mécanisme natif `acpx set reasoning_effort medium` sur la
session avant le prompt — voir `docs/reference/codex-home-isole.md`.

Le preset `kimi` a le même besoin, résolu de la même façon : il pose
`KIMI_CODE_HOME=${REPO}/ops/kimi-home`, un profil versionné possédé par l'app.
L'effort kimi n'est pas non plus passable par appel — le pont l'expose comme un
config option ACP distinct (`thinking`, low|high|max) et acpx ne transmet que
`model`/`allowedTools`/`maxTurns`/`systemPrompt` en one-shot. Le
`[thinking] effort` du profil est donc le seul levier pour un appel `exec`.
Une seule var ici, pas une par rôle : les 5 rôles partagent `high`.

`kimi-code/k3-256k` et `kimi-code/k3` sont le même modèle à deux fenêtres près
(262 144 vs 1 048 576) ; le préfixe `kimi-code/` est le model id annoncé par le
pont, pas un chemin.

## Pièges

- **Redémarrer le daemon.** Le `.env` est lu au boot (`load_dotenv`) ; tant que le
  daemon tourne, il décide avec l'ancien preset. Vérifier ensuite le
  provider et le modèle dans une décision ou un run LLM réellement produit
  après le redémarrage, pas seulement dans le `.env`. `code_version` prouve la
  révision du code, pas le modèle servi.
- **Pas d'assignation gérée hors bloc.** `load_dotenv` garde la **première**
  occurrence d'une clé : une ligne `TRADER_MODEL=…` restée au-dessus du bloc
  gagnerait en silence. Le script retire ces lignes à l'application — c'est la
  raison d'être du nettoyage, pas un effet de bord (test :
  `tests/test_model_preset.py::test_apply_retire_les_assignations_gerees_hors_bloc`).
- **Un preset couvre les 5 rôles.** Un rôle omis retombe sur le défaut *code*
  (`gpt-5.6-terra` pour le brain, `gpt-5.6-sol` pour les analystes) sans rien
  signaler. Un test verrouille cette complétude.
- **Le preset kimi n'a pas de var `CODEX_HOME`** : kimi n'utilise pas le profil
  Codex. Mais la garde s'applique à **tous** les appels acpx, kimi compris — le
  `CODEX_HOME` global du `.env` doit donc rester un profil valide.
- **La garde du transport est agent-aware** (depuis le 2026-08-08) : elle valide
  le profil de l'agent *réellement lancé*. Sous `kimi`, un `KIMI_CODE_HOME`
  illisible ou dont le `[thinking] effort` n'est pas `high`/`max` fait échouer
  l'appel AVANT le subprocess ; la var absente retombe sur `ops/kimi-home`, le
  profil de l'app — jamais sur `~/.kimi-code`. Tests :
  `tests/test_llm.py::test_kimi_*`.
- **`ops/kimi-home` n'est pas versionné en entier** : comme `ops/codex-home`, le
  `.gitignore` ne garde que `config.toml`. Sur une machine neuve, recréer les
  deux symlinks d'auth (`credentials`, `oauth` → `~/.kimi-code/…`), sinon le
  premier appel repart en device-code login.
- **Le profil n'isole pas les skills** (mesuré le 2026-08-08) : kimi
  auto-découvre `~/.claude/skills` et `~/.codex/skills` depuis `$HOME`, hors de
  tout `KIMI_CODE_HOME`, et les injecte dans le system prompt. Sous le mode par
  défaut du transport (`--allowed-tools ""`) elles ne sont pas invocables — du
  contexte mort, pas un levier d'action.

## Voir aussi

- `docs/how-to/manage-model-presets.md` — appliquer, redémarrer et vérifier.
- `docs/reference/codex-home-isole.md` — le profil Codex et la garde d'effort.
- `docs/how-to/run-the-daemon.md` — le `.env` est lu au démarrage.
