# casys-trader

Daemon de trading paper assisté par LLM. Le modèle peut proposer une décision et
un plan ; le code garde l'autorité sur les données, les limites de risque,
l'admission des ordres et la persistance des preuves.

## Démarrage sûr

```bash
uv sync
./run.sh --once          # un cycle sans exécution d'ordre
./run.sh                 # boucle continue sans exécution d'ordre
./run.sh --live --once   # cycle paper avec exécution via le broker simulé
```

Avant un cycle connecté, suivre le [runbook daemon](docs/how-to/run-the-daemon.md).
Ne pas utiliser `--live` sans avoir vérifié le mandat, la configuration de
risque et la connectivité du fournisseur de marché. Le kill switch `KILL` à la
racine bloque toute soumission d'ordre.

## Navigation

| Besoin | Point d'entrée |
|---|---|
| Comprendre les choix et le cycle | [Explications d'architecture](docs/explanation/README.md) |
| Retrouver un invariant ou un format actuel | [Références runtime](docs/reference/README.md) |
| Opérer, relancer ou diagnostiquer | [How-to](docs/how-to/README.md) |
| Apprendre sur un état paper existant | [Tutoriels](docs/tutorials/README.md) |
| Lire les décisions métier datées | [Registre D1–D16](docs/decisions/registre-decisions-metier.md) |
| Connaître la couverture documentaire | [Index de documentation](docs/README.md) |

## Repères stables

- `config/` : univers, risque et paramètres opérateur.
- `mandate/` : mandat et guardrails lus par le runtime.
- `state/` : état et preuves produits à l'exécution ; ne pas l'interpréter sans
  distinguer l'état persistant, le process live et les rapports dérivés.
- `trader/` : domaine, cas d'usage, infrastructure, runtime, agent et interfaces.
- `docs/superpowers/` : specs et plans historiques, utiles comme contexte mais
  non canoniques face au code et aux références runtime.

Pour examiner un résultat paper, commence par le tutoriel
[Suivre une décision jusqu'à sa preuve](docs/tutorials/trace-a-paper-decision.md).
