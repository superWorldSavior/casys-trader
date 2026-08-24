import ast
import subprocess
import sys

from tests.package_layout._helpers import REPO_ROOT


def _makefile_recipe(target: str) -> str:
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    recipe: list[str] = []
    capturing = False
    for line in lines:
        if capturing:
            if line.startswith("\t"):
                recipe.append(line[1:])
                continue
            break
        if line.startswith(f"{target}:"):
            capturing = True
    assert recipe, f"Makefile target {target!r} has no recipe"
    return "\n".join(recipe)


def _python_c_snippet(recipe: str) -> str:
    marker = 'python -c "'
    start = recipe.find(marker)
    assert start != -1, recipe
    start += len(marker)
    end = recipe.find('"', start)
    assert end != -1, recipe
    return recipe[start:end]


def test_live_logs_imports_canonical_supervisor_without_launching() -> None:
    snippet = _python_c_snippet(_makefile_recipe("live-logs"))
    tree = ast.parse(snippet)
    imported_modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ]
    assert "trader.interfaces.cockpit.supervisor" in imported_modules
    assert "trader.cockpit.supervisor" not in imported_modules

    import_only = "; ".join(
        ast.unparse(node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )
    result = subprocess.run(
        [sys.executable, "-c", f"{import_only}; print(L.__module__)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "trader.interfaces.cockpit.supervisor"
