# Référence — CODEX_HOME isolé du daemon

> **Type** : Reference (Diátaxis).
> **Code** : profil versionné `ops/codex-home` · propagation `trader/agent/llm.py::load_dotenv` → `trader/support/system/process_env.py::sanitized_runtime_env` → subprocess `acpx` → pont `codex-acp` (`acpx src/acp/auth-env.ts::buildAgentEnvironment`).
> **Statut** : ✅ **Actif en paper** — profil Codex unique `low` depuis le 2026-08-10 ; effort du brain surchargé par session.
> **Rôle** : donner à tous les appels ACPX de l'app un environnement Codex **nu** (0 plugin, 0 skill, 0 MCP) et imposer un effort de raisonnement **explicite** quel que soit le modèle, pour des sessions de décision **déterministes** au contrat JSON strict, isolées de l'environnement de dev partagé, avec un `exec` optionnel confiné dans un scratch hors repo.

## Un profil, effort du brain par session

Tous les rôles partagent un seul profil nu, `ops/codex-home`, dont le défaut est
`gpt-5.6-sol` avec `model_reasoning_effort = "low"`.

Le modèle se choisit avec l'option `--model` existante d'acpx. Pour le brain,
l'effort se règle ensuite sur la session ACP existante :

```bash
acpx --model gpt-5.6-luna sessions new -s casys-trader:runtime-brain:0
acpx set reasoning_effort medium --session casys-trader:runtime-brain:0
acpx prompt --session casys-trader:runtime-brain:0 '<prompt>'
```

Le trader ne réimplémente pas l'effort : `AcpxBackend.open_session()` appelle
simplement `acpx set` entre la création de session et le premier prompt. La
configuration active est donc : brain `gpt-5.6-luna` medium ; consolidateur,
univers, micro société et macro/news `gpt-5.6-sol` low ; un seul CODEX_HOME.

La garde du profil reste **fail-close** : `ops/codex-home/config.toml` doit porter
explicitement l'effort par défaut `low`, sinon l'appel est refusé avant le
subprocess. L'override medium du brain est lui validé par le pont ACP sur la
session.

## Le problème (ce qui a motivé l'isolation)

Le daemon décide via des sessions `acpx → codex-acp → codex app-server`. Par
défaut, le pont `codex-acp` lit **`~/.codex`**, c'est-à-dire le **même
`CODEX_HOME` que l'environnement de dev** (extension VSCode / ChatGPT). Or ce
`~/.codex/config.toml` déclare **21 plugins/skills** : `superpowers`, `gmail`,
`linear`, `github`, `google-calendar`, `hubspot`, `browser`, `spreadsheets`, etc.

Un de ces skills, **`using-superpowers`**, s'annonce « exigé au démarrage » et
pousse l'agent à **narrer** avant de répondre. Résultat observé : au lieu du JSON
attendu, l'agent produisait de la prose du type

> « J'utilise la compétence superpowers:using-superpowers, exigée au démarrage,
> puis… »

Cette prose **viole le contrat de sortie** (le trader attend du JSON pur) →
`llm_error=parse_error:invalid_json`. Diagnostic 2026-07-06 :

- `parse_error:invalid_json` sur **FRE.DE ×5** (et d'autres), **~10/jour**.
- Reconstruction des 3 tentatives de FRE.DE (toutes `end_turn`, non tronquées) :
  l'une commençait par la prose superpowers, une autre par un JSON aux accolades
  déséquilibrées, la 3ᵉ était propre → défaut **intermittent** de contrat.
- `superpowers` présent dans **412/418 rollouts** Codex du jour.

Aucun de ces 21 plugins n'est nécessaire pour décider un trade. (Les **MCP**
Codex avaient déjà été retirés le 2026-06-17 — cf. note dans
`~/.codex/config.toml` — pour un problème voisin de pileup ; ce chantier retire
les **plugins/skills** restants, pour le daemon uniquement.)

## La solution

Un **`CODEX_HOME` dédié et unique**, versionné dans `ops/codex-home/`, avec un
`config.toml` **nu**. Le daemon le désigne via `.env`. Pour les commandes de
l'app lancées hors daemon, notamment `decisions bench`, le transport retombe
sur ce même répertoire versionné. Avant chaque subprocess, il lit le TOML et
refuse l'appel si `model_reasoning_effort` n'est pas `low` (cf. section
précédente).

Depuis Codex CLI 0.147, un `config.toml` nu ne garantit plus à lui seul un prompt
nu : Codex matérialise ses skills système sous `CODEX_HOME` et découvre aussi
`~/.agents/skills`. Le transport construit donc un `CODEX_CONFIG` par processus
(`acpx_backend._isolated_codex_config`) qui désactive apps/plugins, les agents
collaborateurs et chaque `SKILL.md` découvert. Sur la version runtime épinglée
Codex CLI 0.147, le test `debug prompt-input` montre que l'override effectif doit
viser le chemin exact du fichier `SKILL.md` (le dossier seul ne le désactive pas).
Il fixe aussi
`project_doc_max_bytes = 0` et réinjecte les
seules instructions courtes du runtime à priorité developer, afin que les
`AGENTS.md` d'ingénierie du dépôt n'entrent pas dans une décision marché. Le
`HOME` du subprocess reste inchangé : le stockage de sessions et le cache ACPX
continuent de fonctionner normalement.

### Structure `ops/codex-home/`

| Fichier | Rôle | Suivi git |
| --- | --- | --- |
| `config.toml` | Base nue : `model`, `approval_policy`, `sandbox_mode`, **0 `[plugins.*]`**, **0 `[marketplaces.*]`**, **0 MCP** ; les gates skills/plugins effectifs viennent du `CODEX_CONFIG` runtime | ✅ versionné |
| `auth.json` | Symlink → `~/.codex/auth.json` (auth OpenAI partagée, pas de re-login) | ❌ gitignoré (secret + spécifique machine) |
| `sessions/`, `history/`, `tmp/` | Rollouts générés par le daemon | ❌ gitignoré |

Le `.gitignore` local (`ops/codex-home/.gitignore`) garantit qu'on ne versionne
**que** `config.toml`.

### Propagation (aucune modif d'acpx)

```
.env  CODEX_HOME=…/ops/codex-home
  └─> trader.agent.llm.load_dotenv()            # boot daemon (daemon.py:2249)
        └─> os.environ["CODEX_HOME"]
              └─> sanitized_runtime_env()         # pass-through (process_env.py)
                    └─> subprocess acpx (env=…)    # llm.py::_run_one_shot_command
                          └─> buildAgentEnvironment = { ...process.env }   # acpx src/acp/auth-env.ts
                                └─> pont codex-acp → codex lit $CODEX_HOME/config.toml
```

Le point-clé qui rend l'isolation **simple** : `acpx` construit l'environnement du
pont à partir de `{ ...process.env }` (fichier `src/acp/auth-env.ts`,
`buildAgentEnvironment`). `_run_one_shot_command()` résout et valide donc le
profil de l'app, force ce `CODEX_HOME` dans l'environnement enfant, puis
`sanitized_runtime_env()` retire seulement le bruit système. Un profil dont le
défaut n'est pas `low` échoue avant même de lancer `acpx`. Le brain applique
ensuite `reasoning_effort=medium` via la commande de session ACP documentée plus
haut.

### Exec natif optionnel

`CASYS_AGENT_EXEC=1` autorise les outils natifs Codex (shell/Python) pour les
calculs ad hoc **sur un backend ACPX qui n'a pas interdit l'exec**. Le runtime
impose alors comme cwd `ops/codex-home/calc-scratch/` et refuse de s'ouvrir si
`CODEX_HOME` n'est pas absolu ou si le scratch en sort. Avec
`sandbox_mode = "workspace-write"`, le réseau reste coupé et les écritures sont
confinées à ce scratch gitignoré. Sans le flag, le contrat historique sans
outil natif reste inchangé. Cursor CLI (`ask` + sandbox) n'exécute jamais cet
outil, même si le flag process est levé ; le prompt suit la capacité réelle du
session, pas le flag.

Cette capacité sert au raisonnement numérique, pas à exécuter directement un
ordre : la sortie finale reste du JSON pur, puis le daemon valide et exécute via
ses domain tools et le `RiskGate`. Un script libre ne devient pas non plus une
watch persistante ; les watches restent dans le vocabulaire sémantique gouverné.

Le scratch étant physiquement sous la racine Git, la découverte hiérarchique de
Codex peut aussi voir le `AGENTS.md` du dépôt. Pour empêcher qu'une décision
normale soit prise pour une investigation et déclenche des recherches de fichiers,
`agent_exec_scratch_dir()` matérialise atomiquement un `calc-scratch/AGENTS.md`
plus spécifique : aucune exploration du filesystem, exec réservé aux calculs
numériques fournis par le contexte, aucune écriture et JSON final obligatoire.

## Mise en place / reproduction

```bash
# 1. Home + config nu déjà versionnés dans le repo : ops/codex-home/config.toml
# 2. Auth partagée (symlink, jamais commité) :
ln -sf ~/.codex/auth.json ops/codex-home/auth.json
# 3. Pointer le daemon dessus (chemin ABSOLU) dans .env :
echo 'CODEX_HOME=/chemin/absolu/vers/casys-trader/ops/codex-home' >> .env
# 4. Optionnel : activer les calculs natifs en cage :
echo 'CASYS_AGENT_EXEC=1' >> .env
# 5. Redémarrer le daemon (cf. docs/how-to/run-the-daemon.md) pour recharger .env.
```

Le `.env.example` documente la variable.

## Vérifier que c'est actif

```bash
# Les rollouts du daemon atterrissent dans le home isolé (et plus dans ~/.codex) :
find ops/codex-home/sessions -name '*.jsonl' -newermt "<heure restart>"
# Les instructions runtime terminales ont été matérialisées :
sed -n '1,120p' ops/codex-home/calc-scratch/AGENTS.md
# Et ne contiennent plus la contamination :
grep -rl 'superpowers' ops/codex-home/sessions   # → attendu : vide
```

Preuve de propagation côté trader (reproduit le boot) :

```bash
uv run python -c "import trader.agent.llm as llm; llm.load_dotenv(); \
from trader.support.system.process_env import sanitized_runtime_env as s; \
print(s().get('CODEX_HOME'))"
```

## Pièges & maintenance

- **Chemin absolu** dans `.env` : le pont hérite du cwd d'acpx, un chemin relatif
  est fragile.
- **Ne jamais committer `auth.json`** : c'est un secret et un symlink spécifique à
  la machine. Le `.gitignore` local le protège ; vérifier au `git add`.
- **Auth partagée** : le symlink suit les refresh de token de `~/.codex/auth.json`.
  Si l'auth de dev est purgée, recréer le symlink.
- **Ajouter un plugin sciemment** : le plugin finance **`public-equity-investing`**
  (skills `earnings-deep-dive`, `METRIC_CATALOG`) existe dans le cache Codex et
  pourrait enrichir l'analyse. Mais **tout** plugin risque de réintroduire de la
  narration (re-`parse_error`). Ne l'ajouter au `config.toml` isolé qu'après avoir
  vérifié que le contrat JSON tient (mesurer les `parse_error` avant/après).
- Ce fix est **orthogonal** au watchdog cap-par-appel (`CASYS_ACPX_CALL_TIMEOUT_S`,
  cf. `trader/agent/llm.py`) : l'un nettoie le contrat de sortie, l'autre borne les
  appels figés.

## Voir aussi

- `docs/how-to/run-the-daemon.md` — `CODEX_HOME` est lu au démarrage (comme le
  reste du `.env`).
- `docs/reference/llm-contract.md` — le contrat de sortie que la pollution cassait.
