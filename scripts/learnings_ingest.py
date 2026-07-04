"""Ingestion, scoring et embeddings du store de recall des learnings.

Construit (ou met à jour) ``state/learnings.db`` à partir des archives JSONL,
applique les verdicts FLAIR, calcule les outcome_scores, puis embarque les
embeddings OpenAI.

Usage :
  uv run python scripts/learnings_ingest.py [--no-embeddings] [--state-dir PATH]

Options :
  --no-embeddings   Saute l'étape d'embeddings OpenAI (ingestion + scoring seulement).
                    Utile hors connexion, pour les tests, ou quand OPENAI_API_KEY
                    n'est pas encore disponible.
  --state-dir PATH  Répertoire d'état (défaut : state/ à la racine du projet).

Note : relancer ``scripts/learnings_outcome_bootstrap.py`` avant cet outil si
l'on veut des verdicts frais (forward returns recalculés) avant apply_verdicts.

Bilan JSON sur stdout à la fin — compatible avec ``jq`` ou parsing Python.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI importable pour les tests.

    Charge le .env, ingère les 3 sources JSONL, applique les verdicts FLAIR,
    calcule les outcome_scores, optionnellement backfille les embeddings.
    Affiche le bilan en JSON sur stdout.

    Retourne 0 en succès, 1 en erreur (clé manquante, etc.).
    """
    # Charger .env avant de lire les variables d'environnement
    from trader.agent.llm import load_dotenv  # noqa: PLC0415
    from trader.agent.learnings.store import LearningsStore  # noqa: PLC0415

    load_dotenv()

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Saute l'étape d'embeddings OpenAI.",
    )
    parser.add_argument(
        "--state-dir",
        default=str(ROOT / "state"),
        help="Répertoire d'état (défaut : state/ à la racine du projet).",
    )

    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    archive_dir = state_dir / "archive"

    # Créer le store SQLite (crée le schéma si absent)
    db_path = state_dir / "learnings.db"
    store = LearningsStore(db_path)

    # ------------------------------------------------------------------
    # 1. Ingestion des 3 sources si présentes
    # ------------------------------------------------------------------
    ingest_results: dict[str, dict] = {}

    # Source 1 : archive du ledger (backfill)
    ledger_jsonl = archive_dir / "learnings-from-ledger.jsonl"
    ingest_results["ledger_backfill"] = store.ingest_jsonl(
        ledger_jsonl, source="ledger-backfill"
    )

    # Source 2 : learnings évincés du buffer vif
    evicted_jsonl = archive_dir / "learnings-evicted.jsonl"
    ingest_results["evicted"] = store.ingest_jsonl(evicted_jsonl, source="runtime")

    # Source 3 : buffer vif courant
    runtime_jsonl = state_dir / "learnings.jsonl"
    ingest_results["runtime"] = store.ingest_jsonl(runtime_jsonl, source="runtime")

    # ------------------------------------------------------------------
    # 2. apply_verdicts (bootstrap FLAIR — 0 si fichier absent)
    # ------------------------------------------------------------------
    bootstrap_json = archive_dir / "learnings-outcome-bootstrap.json"
    verdicts_updated = store.apply_verdicts(bootstrap_json)

    # ------------------------------------------------------------------
    # 3. compute_outcome_scores
    # ------------------------------------------------------------------
    scoring_result = store.compute_outcome_scores()

    # ------------------------------------------------------------------
    # 4. backfill_embeddings (sauf si --no-embeddings)
    # ------------------------------------------------------------------
    embeddings_count = 0
    if not args.no_embeddings:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            print(
                "ERREUR : OPENAI_API_KEY non définie. "
                "Utiliser --no-embeddings ou définir la clé dans .env.",
                file=sys.stderr,
            )
            return 1

        from trader.agent.learnings.embeddings import embed_texts  # noqa: PLC0415

        def _embedder(texts: list[str]) -> list[bytes]:
            return embed_texts(texts, api_key=api_key)

        embeddings_count = store.backfill_embeddings(_embedder)

    # ------------------------------------------------------------------
    # 5. Bilan JSON sur stdout
    # ------------------------------------------------------------------
    total_notes = store.count()

    report = {
        "db": str(db_path),
        "ingest": ingest_results,
        "verdicts_updated": verdicts_updated,
        "scoring": scoring_result,
        "embeddings_backfilled": embeddings_count,
        "total_notes": total_notes,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
