"""engine/ must stay business-neutral: it may never import anything from packs/."""
import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
        # Catch sneaky string imports too: importlib.import_module("packs...") or __import__("packs...")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and isinstance(node.args[0].value, str):
                found.append(node.args[0].value)
    return found


def test_engine_imports_nothing_from_packs():
    offenders = [
        f"{path.name}: {name}"
        for path in ENGINE.rglob("*.py")
        for name in _imports_of(path)
        if name == "packs" or name.startswith("packs.")
    ]
    assert offenders == []
