"""Test-first phase helpers.

The model writes tests from the acceptance criteria. The harness then runs
them and, when they fail for a missing implementation rather than a broken
test, locks those files. Later phases cannot edit them.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

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
}
_MAX_SNAPSHOT_BYTES = 1_000_000

_TEST_SUFFIXES = (
    ".test.js",
    ".spec.js",
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
    ".test.jsx",
    ".spec.jsx",
    ".test.mjs",
    ".test.cjs",
)

_BROKEN_TEST = re.compile(
    r"(SyntaxError|IndentationError|TabError|ERROR collecting|error during collection|"
    r"collection error|ImportError while loading conftest)",
    re.IGNORECASE,
)


def is_test_path(rel: str) -> bool:
    """True when `rel` is a test file the test-first phase may lock."""

    text = rel.replace("\\", "/").lstrip("./")
    if not text or text.endswith("/"):
        return False
    parts = [part for part in text.split("/") if part]
    if not parts:
        return False
    name = parts[-1].lower()
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith(("_test.py", "_test.go", "_test.rs")):
        return True
    if name.endswith(_TEST_SUFFIXES):
        return True
    return any(part.lower() in {"test", "tests"} for part in parts[:-1])


def file_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def snapshot_tree(root: Path) -> dict[str, bytes]:
    """Bytes of regular files under `root`, skipping VCS, caches, and binaries."""

    root = Path(root).resolve()
    found: dict[str, bytes] = {}
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in _walk(root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
        for name in filenames:
            path = current / name
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_SNAPSHOT_BYTES:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            rel = path.relative_to(root).as_posix()
            found[rel] = data
    return found


def _walk(root: Path):
    import os

    return os.walk(root)


def changed_rels(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    rels = set(before) | set(after)
    return sorted(rel for rel in rels if before.get(rel) != after.get(rel))


def restore_rels(root: Path, before: dict[str, bytes], rels: list[str]) -> list[str]:
    """Put non-test edits back. Returns the paths that were restored or removed."""

    restored: list[str] = []
    root = Path(root)
    for rel in rels:
        path = root / rel
        if rel in before:
            current = path.read_bytes() if path.is_file() else None
            if current != before[rel]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before[rel])
                restored.append(rel)
        elif path.exists():
            if path.is_file():
                path.unlink()
                restored.append(rel)
            # Directories the model created are left if they are not empty.
    return restored


def classify_test_run(output: str, passed: bool) -> str:
    """Why the test command ended.

    `expected_fail` means the tests ran and failed because the implementation
    is missing or wrong. `broken` means the new tests themselves did not
    collect. `passed_early` means they passed before any implementation.
    """

    if passed:
        return "passed_early"
    if _BROKEN_TEST.search(output or ""):
        return "broken"
    return "expected_fail"
