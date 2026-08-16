"""Archive et purge le stockage des agents (sessions acpx, logs codex).

Trois classes de données, trois traitements :

- **sessions agents** (acpx, codex-home) : transcripts LLM. Précieux pour les
  études a posteriori, mais 46x compressibles en `tar.zst` grâce à la
  redondance inter-fichiers. On archive, on ne jette pas.
- **logs applicatifs** (`ops/codex-home/logs_*.sqlite`) : traces INFO/TRACE du
  process app-server. Aucune valeur post-exécution — purge sèche. Avant la
  purge, ``scripts.llm_cost`` extrait tout usage LLM trouvable (sqlite,
  sessions acpx, homes grok, fallback ``[acpx_call]``) vers
  ``state/archive/llm_usage/``.
- **index et sessions récentes** : jamais touchés.

`--dry-run` est le défaut : l'écriture exige `--apply` (AX #2, safe defaults).
La sortie est un objet JSON (AX #3) pour être exploitable par un agent.

Usage::

    python -m scripts.archive_agent_storage                 # dry-run, tout
    python -m scripts.archive_agent_storage --apply
    python -m scripts.archive_agent_storage --older-than 14 --apply
    python -m scripts.archive_agent_storage --only sessions --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_OLDER_THAN_DAYS = 7
ZSTD_LEVEL = "19"

# launchd/cron n'héritent pas du PATH interactif : résoudre l'exécutable
# explicitement plutôt que d'échouer au milieu d'une archive.
_ZSTD_FALLBACK_PATHS = ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", "/usr/bin/zstd")

ACPX_SESSIONS = Path.home() / ".acpx" / "sessions"
REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = Path.home() / ".acpx" / "archive"
CODEX_HOME = REPO_ROOT / "ops" / "codex-home"

# Ne jamais archiver : l'index vivant et les verrous de sessions actives.
PROTECTED_NAMES = {"index.json"}
PROTECTED_SUFFIXES = {".lock", ".tmp"}


def resolve_zstd() -> str:
    """Chemin de l'exécutable zstd, ou RuntimeError explicite (AX #5, fast fail)."""

    found = shutil.which("zstd")
    if found:
        return found
    for candidate in _ZSTD_FALLBACK_PATHS:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "zstd introuvable (PATH et chemins connus). Installer avec `brew install zstd`, "
        "ou ajouter son répertoire au PATH du job planifié."
    )


def _human(size: int) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} Go"


def _month_key(path: Path) -> str:
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m")


def collect_stale_sessions(root: Path, cutoff: datetime) -> dict[str, list[Path]]:
    """Groupe par mois les fichiers de session plus vieux que ``cutoff``."""

    by_month: dict[str, list[Path]] = defaultdict(list)
    if not root.is_dir():
        return by_month
    cutoff_ts = cutoff.timestamp()
    for path in root.iterdir():
        if not path.is_file():
            continue
        if path.name in PROTECTED_NAMES or path.suffix in PROTECTED_SUFFIXES:
            continue
        if path.stat().st_mtime >= cutoff_ts:
            continue
        by_month[_month_key(path)].append(path)
    return by_month


def archive_month(paths: list[Path], destination: Path, *, apply: bool) -> dict:
    """Écrit ``destination`` (tar.zst) puis supprime les sources. Fail-closed.

    L'archive est écrite dans un fichier temporaire et n'est promue qu'après
    vérification de sa lisibilité : une archive tronquée ne doit jamais
    autoriser la suppression des sources.
    """

    raw_size = sum(p.stat().st_size for p in paths)
    result = {
        "files": len(paths),
        "raw_size": raw_size,
        "raw_size_human": _human(raw_size),
        "archive": str(destination),
        "applied": False,
    }
    if not apply:
        return result

    zstd = resolve_zstd()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(destination.parent)) as tmpdir:
        tar_path = Path(tmpdir) / "sessions.tar"
        with tarfile.open(tar_path, "w") as tar:
            for path in paths:
                tar.add(path, arcname=path.name)
        staged = Path(tmpdir) / destination.name
        subprocess.run(
            [zstd, f"-{ZSTD_LEVEL}", "-T0", "-q", "-o", str(staged), str(tar_path)],
            check=True,
        )
        # Preuve de lisibilité avant de toucher aux sources.
        subprocess.run([zstd, "-t", "-q", str(staged)], check=True)
        shutil.move(str(staged), str(destination))

    for path in paths:
        path.unlink(missing_ok=True)

    packed = destination.stat().st_size
    result.update(
        applied=True,
        archive_size=packed,
        archive_size_human=_human(packed),
        ratio=round(raw_size / packed, 1) if packed else None,
    )
    return result


def _extract_llm_usage_before_purge() -> dict:
    """Best-effort : un échec d'extraction ne doit pas bloquer l'archive."""

    try:
        from scripts.llm_cost import extract_and_append

        return extract_and_append(repo_root=REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - rétention fail-open sur l'observabilité
        return {"error": str(exc), "appended": 0}


def purge_codex_logs(home: Path, *, apply: bool) -> list[dict]:
    """Vide les tables de logs des bases `logs_*.sqlite` puis compacte."""

    out: list[dict] = []
    if not home.is_dir():
        return out
    for db_path in sorted(home.glob("logs_*.sqlite")):
        size = db_path.stat().st_size
        entry = {
            "path": str(db_path),
            "size": size,
            "size_human": _human(size),
            "applied": False,
        }
        try:
            with sqlite3.connect(db_path) as conn:
                entry["rows"] = conn.execute("SELECT count(*) FROM logs").fetchone()[0]
                if apply:
                    conn.execute("DELETE FROM logs")
                    conn.commit()
            if apply:
                # VACUUM exige d'être hors transaction.
                with sqlite3.connect(db_path, isolation_level=None) as conn:
                    conn.execute("VACUUM")
                entry.update(
                    applied=True,
                    size_after=db_path.stat().st_size,
                    size_after_human=_human(db_path.stat().st_size),
                )
        except sqlite3.Error as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than",
        type=int,
        default=DEFAULT_OLDER_THAN_DAYS,
        help=f"âge minimum en jours des sessions à archiver (défaut {DEFAULT_OLDER_THAN_DAYS})",
    )
    parser.add_argument(
        "--only",
        choices=("sessions", "logs"),
        help="ne traiter qu'une classe (défaut : les deux)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="écrire réellement (sans ce drapeau : simulation)",
    )
    args = parser.parse_args(argv)

    if args.older_than < 1:
        parser.error("--older-than doit valoir au moins 1 jour")

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    report: dict = {
        "mode": "apply" if args.apply else "dry-run",
        "cutoff": cutoff.isoformat(),
        "sessions": [],
        "logs": [],
    }

    # Extraire les tokens AVANT l'archive des sessions et la purge sqlite.
    # Additif et idempotent : on le fait aussi en dry-run (rien n'est détruit).
    report["llm_usage"] = _extract_llm_usage_before_purge()

    if args.only != "logs":
        for month, paths in sorted(collect_stale_sessions(ACPX_SESSIONS, cutoff).items()):
            destination = ARCHIVE_ROOT / f"acpx-sessions-{month}.tar.zst"
            if destination.exists():
                # Un mois déjà archivé ne doit pas être écrasé : les sessions
                # restantes de ce mois attendront le prochain nom libre.
                destination = ARCHIVE_ROOT / f"acpx-sessions-{month}.{len(paths)}.tar.zst"
            report["sessions"].append(archive_month(paths, destination, apply=args.apply))

    if args.only != "sessions":
        report["logs"] = purge_codex_logs(CODEX_HOME, apply=args.apply)

    reclaimed = sum(s["raw_size"] for s in report["sessions"]) - sum(
        s.get("archive_size", 0) for s in report["sessions"]
    )
    reclaimed += sum(entry["size"] - entry.get("size_after", entry["size"]) for entry in report["logs"])
    report["reclaimed_human"] = _human(max(0, reclaimed))

    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
