"""Pull project files named in a Python traceback into the fix prompt."""

from __future__ import annotations

import re
from pathlib import Path

from local_agent.context import shrink_text

_FILE_LINE = re.compile(r"""File ["']([^"']+)["'], line (\d+)""")
_SKIP_PARTS = ("site-packages", "dist-packages", "lib/python", "lib\\python", "<frozen", "<string>")


def files_from_traceback(text: str) -> list[str]:
    """Paths in `File "...", line N` order, without duplicates."""

    found: list[str] = []
    seen: set[str] = set()
    for match in _FILE_LINE.finditer(text or ""):
        raw = match.group(1).strip()
        if not raw or raw in seen:
            continue
        if raw.startswith("<"):
            continue
        seen.add(raw)
        found.append(raw)
    return found


def format_traceback_files(
    project: Path,
    text: str,
    *,
    max_files: int = 4,
    per_file: int = 2_000,
    total: int = 8_000,
) -> str:
    """Inlined excerpts of project files cited by a traceback.

    Standard-library and third-party frames are skipped. The result is empty
    when nothing in the project was named.
    """

    project = Path(project).resolve()
    blocks: list[str] = []
    used = 0
    kept = 0
    for raw in files_from_traceback(text):
        if any(part in raw for part in _SKIP_PARTS):
            continue
        path = _inside(project, raw)
        if path is None or not path.is_file():
            continue
        rel = path.relative_to(project).as_posix()
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = shrink_text(body, per_file)
        block = f"### {rel}\n\n```\n{excerpt}\n```\n"
        if used + len(block) > total and blocks:
            break
        blocks.append(block)
        used += len(block)
        kept += 1
        if kept >= max_files:
            break
    return "\n".join(blocks).strip()


def _inside(project: Path, raw: str) -> Path | None:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project / candidate
    try:
        full = candidate.resolve()
    except OSError:
        return None
    try:
        full.relative_to(project)
    except ValueError:
        return None
    return full
