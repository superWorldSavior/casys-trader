# Référence — CODEX_HOME isolé du daemon

> **Type** : Reference (Diátaxis).
> **Code** : `ops/codex-home/config.toml` (versionné) · propagation `trader/agent/llm.py::load_dotenv` → `trader/support/system/process_env.py::sanitized_runtime_env` → subprocess `acpx` → pont `codex-acp` (`acpx src/acp/auth-env.ts::buildAgentEnvironment`).
> **Statut** : ✅ **Actif en paper (2026-07-06, main dabc85b)** — `.env CODEX_HOME=…/ops/codex-home`.
> **Rôle** : donner au daemon un environnement Codex **nu** (0 plugin, 0 skill, 0 MCP) pour des sessions de décision **déterministes** au contrat JSON strict, isolées de l'environnement de dev partagé.

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

Un **`CODEX_HOME` dédié**, versionné dans `ops/codex-home/`, avec un
`config.toml` **nu**. Le daemon le désigne via `.env`, et il se propage jusqu'au
pont sans aucune modification d'`acpx`.

### Structure `ops/codex-home/`

| Fichier | Rôle | Suivi git |
| --- | --- | --- |
| `config.toml` | Config nue : `model`, `approval_policy`, `sandbox_mode`, **0 `[plugins.*]`**, **0 `[marketplaces.*]`**, **0 MCP** | ✅ versionné |
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

Le point-clé qui rend le fix **facile** : `acpx` construit l'environnement du
pont à partir de `{ ...process.env }` (fichier `src/acp/auth-env.ts`,
`buildAgentEnvironment`). Il suffit donc que le process `acpx` parent porte
`CODEX_HOME` — ce que `sanitized_runtime_env()` assure (elle ne filtre que les
préfixes `MALLOC_` et le `PATH`, pas `CODEX_HOME`).

## Mise en place / reproduction

```bash
# 1. Home + config nu déjà versionnés dans le repo : ops/codex-home/config.toml
# 2. Auth partagée (symlink, jamais commité) :
ln -sf ~/.codex/auth.json ops/codex-home/auth.json
# 3. Pointer le daemon dessus (chemin ABSOLU) dans .env :
echo 'CODEX_HOME=/chemin/absolu/vers/casys-trader/ops/codex-home' >> .env
# 4. Redémarrer le daemon (cf. docs/how-to/run-the-daemon.md) pour recharger .env.
```

Le `.env.example` documente la variable.

## Vérifier que c'est actif

```bash
# Les rollouts du daemon atterrissent dans le home isolé (et plus dans ~/.codex) :
find ops/codex-home/sessions -name '*.jsonl' -newermt "<heure restart>"
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
