"""Adaptateur fichier pour la rotation gzip des ledgers JSONL.

D-c — rotation transparente pour le daemon (exécutée au démarrage, pas en cours
de run). Les lecteurs d'analyse lisent archives + fichier vif via
``read_rows_with_archive``. Les lecteurs runtime (TUI tail-50,
``_tail_decisions_safe``) ne sont PAS modifiés — ils opèrent sur le fichier vif.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import zlib
from datetime import datetime
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


def _month_prefix(ts_raw: object) -> str | None:
    """Extrait le préfixe YYYY-MM d'un timestamp ISO. Retourne None si invalide."""
    if not ts_raw:
        return None
    try:
        value = str(ts_raw).strip()
        # ISO 8601 : les 7 premiers caractères sont YYYY-MM et l'index 7 doit être '-'
        if len(value) >= 8 and value[4] == "-" and value[7] == "-":
            return value[:7]
    except Exception:  # noqa: BLE001
        pass
    return None


def rotate_monthly(
    path: Path,
    archive_dir: Path,
    *,
    now: datetime,
    ts_key: str,
) -> dict:
    """Partitionne un fichier JSONL par mois et archive les mois passés en gzip.

    Stratégie :
    - Lit le fichier ligne à ligne (STREAMING) — jamais ``read_text()``.
    - Lignes sans ts valide : restent dans le fichier vif.
    - Mois passés (< YYYY-MM de ``now``) : archivés en gzip vers
      ``archive_dir/<stem>-YYYY-MM.jsonl.gz``.
    - Si l'archive du mois existe déjà : append en mode ``'ab'``
      (membres gzip concaténés, relisibles par ``gzip.open``).
    - Fichier vif réécrit atomiquement (tmp + ``os.replace``) avec
      les lignes du mois courant et les lignes sans ts valide.

    Paramètres
    ----------
    path:
        Fichier JSONL à tourner (p. ex. ``state/decisions.jsonl``).
    archive_dir:
        Répertoire cible des archives (p. ex. ``state/archive/``).
    now:
        Référence temporelle pour déterminer le mois courant.
    ts_key:
        Nom du champ timestamp dans chaque row JSON
        (``"cycle_ts"`` pour decisions, ``"ts"`` pour events).

    Retourne
    --------
    dict avec ``archived`` (lignes archivées), ``kept`` (lignes conservées dans
    le vif) et ``files`` (liste des chemins d'archives écrits/augmentés).
    """
    if not path.exists():
        return {"archived": 0, "kept": 0, "files": []}

    current_month = f"{now.year:04d}-{now.month:02d}"

    # Partition : mois passés → à archiver ; mois courant / sans ts → conserver
    by_month: dict[str, list[bytes]] = {}  # YYYY-MM → lignes à archiver
    kept_lines: list[bytes] = []  # lignes à conserver dans le vif

    with path.open("rb") as fh:
        for raw_line in fh:
            stripped = raw_line.rstrip(b"\r\n")
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                # Ligne corrompue → reste dans le vif
                kept_lines.append(stripped + b"\n")
                continue

            if not isinstance(row, dict):
                kept_lines.append(stripped + b"\n")
                continue

            month = _month_prefix(row.get(ts_key))
            if month is None or month >= current_month:
                kept_lines.append(stripped + b"\n")
            else:
                by_month.setdefault(month, []).append(stripped + b"\n")

    archived = 0
    written_files: list[str] = []
    # Lignes des mois dont l'archivage a échoué — remises dans le vif.
    extra_kept: list[bytes] = []

    if by_month:
        archive_dir.mkdir(parents=True, exist_ok=True)
        stem = path.stem  # "decisions" ou "events"
        for month in sorted(by_month):
            new_lines = by_month[month]
            archive_path = archive_dir / f"{stem}-{month}.jsonl.gz"
            tmp_archive = archive_path.parent / (archive_path.name + ".tmp")

            # ── Étape 1 : lire l'archive existante (streaming) ──────────────
            existing_lines: list[bytes] = []
            if archive_path.exists():
                try:
                    with gzip.open(archive_path, "rb") as gz:
                        for raw in gz:
                            stripped = raw.rstrip(b"\r\n")
                            if stripped:
                                existing_lines.append(stripped + b"\n")
                except (OSError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
                    # Archive corrompue → abort ce mois, vif intact pour ces lignes.
                    log.error(
                        "archive ledger corrompue — rotation abortée pour %s (%s); vif intact",
                        archive_path,
                        exc,
                    )
                    extra_kept.extend(new_lines)
                    continue

            # ── Étape 2 : fusionner + dédupliquer ───────────────────────────
            # Priorité clé : decision_id quand présent ; sinon bytes bruts exacts.
            seen_ids: set[str] = set()
            seen_raw: set[bytes] = set()
            deduped: list[bytes] = []
            for raw_line in existing_lines + new_lines:
                stripped = raw_line.rstrip(b"\r\n")
                try:
                    row = json.loads(stripped)
                    decision_id = row.get("decision_id") if isinstance(row, dict) else None
                except json.JSONDecodeError:
                    decision_id = None

                if decision_id:
                    key = str(decision_id)
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                else:
                    if stripped in seen_raw:
                        continue
                    seen_raw.add(stripped)
                deduped.append(stripped + b"\n")

            # ── Étapes 3–5 : écriture tmp → validation → os.replace ─────────
            try:
                with gzip.open(tmp_archive, "wb") as gz:
                    for line in deduped:
                        gz.write(line)
                # Validation : relecture complète du tmp avant de le promouvoir.
                with gzip.open(tmp_archive, "rb") as gz:
                    for _ in gz:
                        pass
                os.replace(tmp_archive, archive_path)
            except Exception as exc:
                try:
                    tmp_archive.unlink(missing_ok=True)
                except OSError:
                    pass
                log.error(
                    "écriture archive %s échouée — rotation abortée (%s)",
                    archive_path,
                    exc,
                )
                extra_kept.extend(new_lines)
                continue

            archived += len(new_lines)
            if str(archive_path) not in written_files:
                written_files.append(str(archive_path))

    # Les lignes des mois avortés retournent dans le vif.
    all_kept = kept_lines + extra_kept

    # Réécriture atomique du fichier vif — EN DERNIER (après toutes les archives).
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("wb") as fh:
            for line in all_kept:
                fh.write(line)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return {"archived": archived, "kept": len(all_kept), "files": written_files}


def read_rows_with_archive(
    path: Path,
    archive_dir: Path,
) -> Iterator[dict]:
    """Itère toutes les rows du ledger : archives gzip (chronologiques) puis fichier vif.

    Les archives sont lues dans l'ordre alphabétique du nom (= ordre chronologique
    YYYY-MM). Les lignes non-JSON ou non-dict sont silencieusement ignorées.
    Une archive individuelle illisible (corruption post-crash) est sautée avec
    un warning — jamais d'exception qui casserait toute l'analyse.

    Utilisation typique (analyse historique) ::

        rows = list(ledger_rotation.read_rows_with_archive(
            state_dir / "decisions.jsonl",
            state_dir / "archive",
        ))
    """
    stem = path.stem
    if archive_dir.exists():
        for archive_path in sorted(archive_dir.glob(f"{stem}-????-??.jsonl.gz")):
            try:
                with gzip.open(archive_path, "rt", encoding="utf-8") as gz:
                    for line in gz:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(row, dict):
                            yield row
            except (OSError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
                # zlib.error = cas réel d'un membre gzip tronqué par un crash
                # pendant la rotation (vérifié : 4 troncatures sur 6 lèvent
                # zlib.error, pas BadGzipFile). On saute l'archive abîmée mais
                # on le DIT — une archive ignorée en silence fausse l'analyse.
                log.warning("archive ledger illisible, ignorée: %s (%s)", archive_path, exc)
                continue

    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
