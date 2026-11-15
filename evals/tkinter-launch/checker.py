"""Hidden check: path join works, and pack/grid are not mixed."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(project))
    import app

    try:
        got = app.output_file(Path("data"), "items.txt")
    except Exception as exc:
        print(f"output_file raised {type(exc).__name__}: {exc}")
        return 1
    if got != Path("data") / "items.txt":
        print(f"output_file returned {got!r}")
        return 1
    if mixes_pack_and_grid(project / "app.py"):
        print("pack and grid are both used")
        return 1
    print("ok")
    return 0


def mixes_pack_and_grid(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        body = node.body if isinstance(node, ast.Module) else node.body
        names: set[str] = set()
        for child in ast.walk(ast.Module(body=body, type_ignores=[])):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                continue
            if child.func.attr in {"pack", "grid"}:
                names.add(child.func.attr)
        if "pack" in names and "grid" in names:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
