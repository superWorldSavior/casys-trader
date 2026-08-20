#!/usr/bin/env python3
"""Bascule le jeu de modèles LLM du daemon entre presets nommés.

Les 5 rôles (brain, consolidateur, univers, micro société, macro/news) sont
configurés par des paires ``TRADER_*_ACPX_AGENT`` / ``TRADER_*_MODEL`` dans le
``.env``. Le brain peut en plus fixer son effort via
``TRADER_REASONING_EFFORT``. Changer de famille de modèles à la main = éditer 10 lignes sans en
oublier une. Ce script écrit ce jeu comme un BLOC délimité du ``.env``, à partir
d'un preset versionné dans ``ops/model-presets/*.env`` — un preset abandonné
reste donc un fichier, pas des lignes commentées qu'on finit par perdre.

Usage :
    python scripts/model_preset.py list             # presets disponibles
    python scripts/model_preset.py show             # preset actif + jeu résolu
    python scripts/model_preset.py apply kimi       # DRY-RUN (défaut) : diff seul
    python scripts/model_preset.py apply kimi --write   # écrit .env (+ .env.bak)
    python scripts/model_preset.py show --json      # sortie machine

Écrire n'est jamais implicite (``--write``), et l'écriture ne prend effet qu'au
REDÉMARRAGE du daemon : le ``.env`` est lu au boot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRESET_DIR = REPO_ROOT / "ops" / "model-presets"
ENV_PATH = REPO_ROOT / ".env"

BEGIN_MARK = "# >>> casys:model-preset"
END_MARK = "# <<< casys:model-preset <<<"
_BEGIN_RE = re.compile(r"^# >>> casys:model-preset=(?P<name>[\w.-]+) >>>")
_ASSIGN_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


class PresetError(RuntimeError):
    """Erreur exploitable par un appelant : code + contexte, pas de la prose."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def preset_path(name: str) -> Path:
    return PRESET_DIR / f"{name}.env"


def available_presets() -> list[str]:
    if not PRESET_DIR.is_dir():
        return []
    return sorted(p.stem for p in PRESET_DIR.glob("*.env"))


def parse_env_text(text: str) -> dict[str, str]:
    """Paires clé=valeur d'un texte .env, commentaires et lignes vides ignorés."""

    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip().strip('"').strip("'")
    return pairs


def load_preset(name: str) -> dict[str, str]:
    path = preset_path(name)
    if not path.is_file():
        raise PresetError(
            "preset_not_found",
            f"preset {name!r} introuvable dans {PRESET_DIR} — disponibles : {available_presets()}",
        )
    # ${REPO} → racine absolue du dépôt pour les profils isolés versionnés.
    text = path.read_text(encoding="utf-8").replace("${REPO}", str(REPO_ROOT))
    pairs = parse_env_text(text)
    if not pairs:
        raise PresetError("preset_empty", f"preset {name!r} ne déclare aucune variable : {path}")
    return pairs


def managed_keys() -> set[str]:
    """Union des clés de TOUS les presets.

    C'est l'union, pas les clés du seul preset cible : une clé posée par un
    preset et absente du suivant doit disparaître du ``.env`` à la bascule,
    sinon elle survivrait en silence (par exemple l'effort du brain Codex
    resterait actif sous un preset kimi).
    """

    keys: set[str] = set()
    for name in available_presets():
        keys |= set(load_preset(name))
    return keys


def split_env(text: str) -> tuple[list[str], str | None, list[str]]:
    """Découpe le .env en (avant-bloc, nom du preset actif, après-bloc)."""

    lines = text.splitlines()
    start = end = None
    active: str | None = None
    for i, line in enumerate(lines):
        if start is None and line.startswith(BEGIN_MARK):
            start = i
            match = _BEGIN_RE.match(line)
            active = match.group("name") if match else "?"
        elif start is not None and line.startswith(END_MARK):
            end = i
            break
    if start is None:
        return lines, None, []
    if end is None:
        raise PresetError(
            "block_unterminated",
            f"bloc preset ouvert ligne {start + 1} de {ENV_PATH} mais jamais fermé "
            f"par {END_MARK!r} — refus d'écrire pour ne pas corrompre le .env",
        )
    return lines[:start], active, lines[end + 1 :]


def render_block(name: str, pairs: dict[str, str]) -> list[str]:
    return [
        f"# >>> casys:model-preset={name} >>>",
        f"# Bloc GÉNÉRÉ par scripts/model_preset.py — éditer ops/model-presets/{name}.env, pas ici.",
        *(f"{key}={value}" for key, value in pairs.items()),
        END_MARK,
    ]


def plan_apply(name: str) -> dict:
    """Calcule la bascule sans rien écrire (le dry-run et l'écriture partagent ce plan)."""

    pairs = load_preset(name)
    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.is_file() else ""
    before, active, after = split_env(env_text)
    current = parse_env_text(env_text)
    managed = managed_keys()

    # Une clé gérée qui traîne HORS bloc gagnerait sur le bloc : le loader du
    # daemon (trader.agent.llm.load_dotenv) garde la PREMIÈRE occurrence.
    stripped = [line for line in (*before, *after) if (m := _ASSIGN_RE.match(line)) and m.group("key") in managed]
    kept_before = [line for line in before if not ((m := _ASSIGN_RE.match(line)) and m.group("key") in managed)]
    kept_after = [line for line in after if not ((m := _ASSIGN_RE.match(line)) and m.group("key") in managed)]

    changes = [
        {"key": key, "from": current.get(key), "to": value} for key, value in pairs.items() if current.get(key) != value
    ]
    removed = [{"key": key, "from": current[key]} for key in sorted(managed - set(pairs)) if key in current]

    body = [*kept_before]
    if body and body[-1].strip():
        body.append("")
    body += render_block(name, pairs)
    body += kept_after
    new_text = "\n".join(body).rstrip("\n") + "\n"

    return {
        "preset": name,
        "active": active,
        "changes": changes,
        "removed_keys": removed,
        "stripped_lines": stripped,
        "new_text": new_text,
    }


def cmd_list(args: argparse.Namespace) -> int:
    names = available_presets()
    if args.json:
        print(json.dumps({"presets": names, "dir": str(PRESET_DIR)}, indent=2))
        return 0
    for name in names:
        print(name)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    env_text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.is_file() else ""
    _, active, _ = split_env(env_text)
    current = parse_env_text(env_text)
    resolved = {key: current.get(key) for key in sorted(managed_keys()) if key in current}
    if args.json:
        print(json.dumps({"active": active, "resolved": resolved}, indent=2))
        return 0
    print(f"preset actif : {active or '(aucun bloc — config manuelle)'}")
    for key, value in resolved.items():
        print(f"  {key}={value}")
    return 0


def _atomic_write_private(path: Path, text: str) -> None:
    """Remplace ``path`` atomiquement par un fichier privé et durable."""

    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def cmd_apply(args: argparse.Namespace) -> int:
    plan = plan_apply(args.name)
    if args.json:
        print(json.dumps({k: v for k, v in plan.items() if k != "new_text"}, indent=2))
    else:
        print(f"preset : {plan['active'] or '(aucun)'} -> {plan['preset']}")
        for change in plan["changes"]:
            print(f"  {change['key']}: {change['from'] or '(absent)'} -> {change['to']}")
        for entry in plan["removed_keys"]:
            print(f"  {entry['key']}: {entry['from']} -> (retiré)")
        if not plan["changes"] and not plan["removed_keys"]:
            print("  (aucun changement)")

    if not args.write:
        if not args.json:
            print("[dry-run] rejouer avec --write pour appliquer")
        return 0

    backup = ENV_PATH.parent / ".env.bak"
    if ENV_PATH.is_file():
        _atomic_write_private(backup, ENV_PATH.read_text(encoding="utf-8"))
    ENV_PATH.write_text(plan["new_text"], encoding="utf-8")
    if not args.json:
        print(f"écrit : {ENV_PATH} (sauvegarde : {backup})")
        print("⚠️  REDÉMARRER le daemon : le .env est lu au boot.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="sortie machine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="liste les presets versionnés")
    sub.add_parser("show", help="preset actif et jeu de modèles résolu")

    apply_parser = sub.add_parser("apply", help="bascule vers un preset (dry-run par défaut)")
    apply_parser.add_argument("name", help="nom du preset (cf. list)")
    apply_parser.add_argument("--write", action="store_true", help="écrit réellement le .env")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"list": cmd_list, "show": cmd_show, "apply": cmd_apply}
    try:
        return handlers[args.command](args)
    except PresetError as exc:
        payload = {"error": exc.code, "message": str(exc)}
        print(json.dumps(payload) if args.json else f"[{exc.code}] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
