"""Build the Project context block that every phase sees first.

The block is prepended to the first user message. Context compaction keeps
that message, so AGENTS.md and the task's context files are not dropped when
later tool output is trimmed.
"""

from __future__ import annotations

from pathlib import Path

from local_agent.context import estimate_tokens, prompt_budget
from local_agent.prompts import system_prompt

DEFAULT_CONTEXT_CHARS = 14_000
_TRUNCATED = "[truncated - read {path} for the rest]"


def with_project_context(prompt: str, block: str) -> str:
    """Put the context block at the start of a phase's first user message."""

    if not block.strip():
        return prompt
    return block.rstrip() + "\n\n" + prompt


def build_project_context(
    project: Path,
    context_paths: list[str],
    *,
    limit: int = DEFAULT_CONTEXT_CHARS,
) -> str:
    """AGENTS.md, then each context path, capped at `limit` characters.

    Every path is listed even when a file is cut, so the model can read the
    rest with the read tool.
    """

    project = project.resolve()
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    agents = project / "AGENTS.md"
    if agents.is_file():
        entries.append(("AGENTS.md", _read_text(agents)))
        seen.add("AGENTS.md")
    for raw in context_paths:
        rel = raw.replace("\\", "/").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        try:
            path = _inside_project(project, rel)
        except ValueError as exc:
            entries.append((rel, f"(not read: {exc})"))
            continue
        if not path.is_file():
            entries.append((rel, "(file not found; read it later if a previous task creates it)"))
            continue
        entries.append((rel, _read_text(path)))
    if not entries:
        return ""
    return _pack(entries, limit)


def context_fits_budget(block: str, *, context_tokens: int, generation_tokens: int) -> bool:
    """True when system prompt + context block fit in the reply's prompt budget."""

    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": block},
    ]
    return estimate_tokens(messages) <= prompt_budget(context_tokens, generation_tokens)


def shrink_context_to_budget(block: str, *, context_tokens: int, generation_tokens: int) -> str:
    """Last-resort cut so a huge cap cannot crowd out the reply on the retry budget."""

    if context_fits_budget(block, context_tokens=context_tokens, generation_tokens=generation_tokens):
        return block
    overhead = estimate_tokens(
        [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": ""},
        ]
    )
    char_budget = max(0, (prompt_budget(context_tokens, generation_tokens) - overhead) * 4)
    note = "\n[truncated - read AGENTS.md for the rest]\n"
    keep = max(0, char_budget - len(note))
    return block[:keep] + note


def _inside_project(project: Path, rel: str) -> Path:
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError(f"path must be relative to the project: {rel}")
    full = (project / candidate).resolve()
    try:
        full.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"path escapes the project: {rel}") from exc
    return full


def _read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"(could not read: {exc})"
    if b"\0" in data[:8192]:
        return f"(binary file, {len(data)} bytes; not inlined)"
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _pack(entries: list[tuple[str, str]], limit: int) -> str:
    names = [name for name, _text in entries]
    listing = "Project context\n\nPaths (read any path that was cut):\n" + "\n".join(
        f"- {name}" for name in names
    )
    bodies = []
    for name, text in entries:
        bodies.append(f"## {name}\n\n{text.rstrip()}\n")
    full = listing + "\n\n" + "\n".join(bodies)
    if len(full) <= limit:
        return full.rstrip() + "\n"
    # Share the remaining characters across files so each path stays visible.
    overhead = len(listing) + sum(len(name) + len(_TRUNCATED.format(path=name)) + 16 for name in names)
    share = max(80, (limit - overhead) // max(1, len(entries)))
    parts = [listing, ""]
    for name, text in entries:
        note = _TRUNCATED.format(path=name)
        body = text.rstrip()
        piece = f"## {name}\n\n{body}\n"
        if len(piece) > share:
            keep = max(0, share - len(note) - 8)
            piece = f"## {name}\n\n{body[:keep].rstrip()}\n{note}\n"
        parts.append(piece)
    packed = "\n".join(parts).rstrip() + "\n"
    if len(packed) <= limit:
        return packed
    # The path list itself is the part that must survive.
    note = "\n[truncated - read AGENTS.md for the rest]\n"
    return (packed[: max(0, limit - len(note))] + note)[:limit]
