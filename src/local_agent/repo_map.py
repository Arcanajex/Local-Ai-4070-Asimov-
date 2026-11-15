"""A short map of Python modules, classes, and functions.

The map is appended to the project context so a fresh phase can see the
shape of the repo without reading every file. It is capped at about 1.5k
tokens (6,000 characters). Syntax errors are listed and skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_MAP_CHARS = 6_000
_SKIP_DIRS = {
    ".git",
    ".agent",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    "runtime",
    "dist",
    "build",
    ".tox",
}
_MAX_FILES = 80
_MAX_SYMBOLS = 12


def build_repo_map(project: Path, *, limit_chars: int = REPO_MAP_CHARS) -> str:
    """Return a markdown list, or "" when the tree has no Python files."""

    project = Path(project).resolve()
    if not project.is_dir():
        return ""
    files = _python_files(project)
    if not files:
        return ""
    lines = ["## Repo map", ""]
    truncated = False
    for path in files:
        rel = path.relative_to(project).as_posix()
        lines.append(_describe(path, rel))
        if sum(len(line) + 1 for line in lines) > limit_chars:
            truncated = True
            break
    if truncated:
        lines.append("- [repo map truncated]")
    text = "\n".join(lines).strip() + "\n"
    if len(text) > limit_chars:
        note = "\n- [repo map truncated]\n"
        text = text[: max(0, limit_chars - len(note))].rstrip() + note
    return text


def _python_files(project: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in __import__("os").walk(project):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
            if len(found) >= _MAX_FILES:
                return sorted(found)
    return sorted(found)


def _describe(path: Path, rel: str) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return f"- {rel} (unreadable: {exc.__class__.__name__})"
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return f"- {rel} (syntax error)"
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"{node.name}()")
        elif isinstance(node, ast.ClassDef):
            methods = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_")
            ]
            if methods:
                symbols.append(f"class {node.name} [{', '.join(methods[:6])}]")
            else:
                symbols.append(f"class {node.name}")
        if len(symbols) >= _MAX_SYMBOLS:
            break
    if not symbols:
        return f"- {rel}"
    return f"- {rel}: " + "; ".join(symbols)
