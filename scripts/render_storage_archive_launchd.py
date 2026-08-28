"""Render the local storage-retention LaunchAgent from its tracked template."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

REPO_PLACEHOLDER = "/ABSOLUTE/PATH/TO/casys-trader"
UV_PLACEHOLDER = "/opt/homebrew/bin/uv"
PATH_PLACEHOLDER = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG_NAMES = ("storage-archive.log", "storage-archive.err.log")
JSONL_LOG_NAME = "storage-archive.log"
_CHILD_TOOL_NAMES = ("zstd", "acpx", "node")
_CHILD_TOOL_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def launchd_child_tool_path(*, uv_binary: Path) -> str:
    """PATH for tools the job may spawn (uv/zstd/acpx/node) plus system dirs."""

    parts: list[str] = []
    seen: set[str] = set()

    def add(raw: str | Path) -> None:
        text = str(raw)
        if not text:
            return
        normalized = os.path.normpath(text)
        if normalized in seen:
            return
        seen.add(normalized)
        parts.append(normalized)

    uv_dir = uv_binary.expanduser().absolute().parent
    add(uv_dir)
    search_path = os.environ.get("PATH", os.defpath)
    for name in _CHILD_TOOL_NAMES:
        found = shutil.which(name, path=search_path)
        if found:
            add(Path(found).parent)
            continue
        sibling = uv_dir / name
        if sibling.is_file():
            add(uv_dir)
            continue
        for prefix in _CHILD_TOOL_FALLBACK_DIRS:
            candidate = Path(prefix) / name
            if candidate.is_file():
                add(prefix)
                break
    for part in os.defpath.split(os.pathsep):
        add(part)
    if not parts:
        raise ValueError("child tool PATH must not be empty")
    return os.pathsep.join(parts)


def render_launchd_template(
    template: str,
    *,
    repo_root: Path,
    uv_binary: Path,
    child_tool_path: str | None = None,
) -> str:
    """Resolve the only machine-local values and reject an unexpected template."""

    if REPO_PLACEHOLDER not in template:
        raise ValueError(f"missing repository placeholder: {REPO_PLACEHOLDER}")
    if UV_PLACEHOLDER not in template:
        raise ValueError(f"missing uv placeholder: {UV_PLACEHOLDER}")
    if PATH_PLACEHOLDER not in template:
        raise ValueError(f"missing PATH placeholder: {PATH_PLACEHOLDER}")
    path_value = child_tool_path if child_tool_path is not None else launchd_child_tool_path(uv_binary=uv_binary)
    if not path_value.strip():
        raise ValueError("child tool PATH must not be empty")
    return (
        template.replace(REPO_PLACEHOLDER, xml_escape(str(repo_root.resolve())))
        .replace(UV_PLACEHOLDER, xml_escape(str(uv_binary.expanduser().absolute())))
        .replace(PATH_PLACEHOLDER, xml_escape(path_value))
    )


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _looks_like_jsonl(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return True
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False
    return True


def prepare_storage_archive_logs(log_dir: Path) -> dict:
    """Create a private log directory and rotate legacy pretty-JSON reports before JSONL."""

    if log_dir.exists() and _is_symlink(log_dir):
        raise RuntimeError("storage archive log directory must not be a symlink")
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _is_symlink(log_dir):
        raise RuntimeError("storage archive log directory must not be a symlink")
    os.chmod(log_dir, 0o700)
    rotated: list[str] = []
    for name in LOG_NAMES:
        path = log_dir / name
        if path.exists() and _is_symlink(path):
            raise RuntimeError(f"storage archive log must not be a symlink: {path}")
        if path.is_file() and name == JSONL_LOG_NAME and not _looks_like_jsonl(path):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = path.with_name(f"{path.name}.legacy-{stamp}")
            os.rename(path, destination)
            os.chmod(destination, 0o600)
            rotated.append(str(destination))
        if not path.exists():
            path.touch()
        os.chmod(path, 0o600)
    return {"root": str(log_dir), "rotated": rotated}


def write_launchd_plist(
    *,
    template_path: Path,
    output_path: Path,
    repo_root: Path,
    uv_binary: Path,
    child_tool_path: str | None = None,
) -> None:
    rendered = render_launchd_template(
        template_path.read_text(encoding="utf-8"),
        repo_root=repo_root,
        uv_binary=uv_binary,
        child_tool_path=child_tool_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template",
        type=Path,
        default=repo_root / "ops" / "launchd" / "ai.casys.trader.storage-archive.plist.example",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "ops" / "launchd" / "ai.casys.trader.storage-archive.plist",
    )
    args = parser.parse_args(argv)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv executable not found in PATH")
    uv_binary = Path(uv)
    child_tool_path = launchd_child_tool_path(uv_binary=uv_binary)
    write_launchd_plist(
        template_path=args.template,
        output_path=args.output,
        repo_root=repo_root,
        uv_binary=uv_binary,
        child_tool_path=child_tool_path,
    )
    logs = prepare_storage_archive_logs(repo_root / "state" / "logs")
    print(
        json.dumps(
            {
                "schema_version": "storage_archive_launchd.v1",
                "output": str(args.output),
                "working_directory": str(repo_root),
                "uv": str(uv_binary.expanduser().absolute()),
                "child_tool_path": child_tool_path,
                "logs": logs,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
