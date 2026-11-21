"""Build the Project context block that every phase sees first.

The block is prepended to the first user message. Context compaction keeps
that message, so AGENTS.md and the task's context files are not dropped when
later tool output is trimmed.

A long AGENTS.md is not cut as a raw prefix. Sections whose headings match
the task or a cheat-sheet marker are kept, and the headings that did not fit
are named. The repo map is attached beside this block, in its own budget, so
a symbol index cannot push AGENTS.md out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from local_agent.context import estimate_tokens, prompt_budget
from local_agent.prompts import system_prompt

DEFAULT_CONTEXT_CHARS = 14_000
_TRUNCATED = "[truncated - read {path} for the rest]"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FOCUS_WORD = re.compile(r"[A-Za-z_][\w./\\-]{3,}")
_FOCUS_STOP = {
    "this",
    "that",
    "with",
    "from",
    "task",
    "file",
    "files",
    "when",
    "then",
    "true",
    "false",
    "none",
    "goal",
    "acceptance",
    "criteria",
    "scope",
    "should",
    "must",
    "into",
    "your",
    "have",
    "does",
    "will",
    "the",
    "and",
    "for",
    "not",
    "are",
    "was",
    "what",
    "which",
    "while",
    "where",
    "after",
    "before",
    "about",
    "there",
    "their",
    "would",
    "could",
    "change",
    "update",
    "edit",
    "write",
    "read",
    "using",
    "class",
    "method",
}


@dataclass
class ContextFileNote:
    path: str
    original_chars: int
    kept_chars: int
    truncated: bool
    omitted_headings: list[str] = field(default_factory=list)


@dataclass
class ProjectContextPack:
    text: str
    files: list[ContextFileNote] = field(default_factory=list)
    hard_cut: bool = False


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
    focus: str = "",
    markers: list[str] | tuple[str, ...] | None = None,
) -> str:
    """AGENTS.md, then each context path, capped at `limit` characters.

    Every path is listed even when a file is cut, so the model can read the
    rest with the read tool.
    """

    return pack_project_context(
        project,
        context_paths,
        limit=limit,
        focus=focus,
        markers=markers,
    ).text


def pack_project_context(
    project: Path,
    context_paths: list[str],
    *,
    limit: int = DEFAULT_CONTEXT_CHARS,
    focus: str = "",
    markers: list[str] | tuple[str, ...] | None = None,
) -> ProjectContextPack:
    """Same block as `build_project_context`, plus per-file truncation notes."""

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
        return ProjectContextPack("")
    return _pack(entries, limit, focus=focus, markers=markers)


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


def attach_repo_map(
    project_text: str,
    repo_map: str,
    *,
    context_tokens: int,
    generation_tokens: int,
) -> str:
    """Append one repo map without letting it shrink the project context.

    The map is replaced, not duplicated, when this is called on a block that
    already has one. If the combined text does not fit the prompt budget, the
    map is shortened and the project context is left as it was.
    """

    base = _strip_repo_map(project_text).rstrip()
    base = (base + "\n") if base else ""
    if not repo_map.strip():
        return base
    if base and not context_fits_budget(base, context_tokens=context_tokens, generation_tokens=generation_tokens):
        return base
    full = (base.rstrip() + "\n\n" + repo_map.strip() + "\n") if base.strip() else (repo_map.strip() + "\n")
    if context_fits_budget(full, context_tokens=context_tokens, generation_tokens=generation_tokens):
        return full
    base_tokens = estimate_tokens(
        [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": base},
        ]
    )
    room_tokens = prompt_budget(context_tokens, generation_tokens) - base_tokens
    room_chars = max(0, room_tokens * 4 - 8)
    if room_chars < 80:
        return base
    note = "\n- [repo map truncated]\n"
    snippet = repo_map.strip()[: max(0, room_chars - len(note))].rstrip() + note
    return base.rstrip() + "\n\n" + snippet


def assemble_phase_context(
    project_text: str,
    repo_map: str,
    *,
    prior: str = "",
    context_tokens: int,
    generation_tokens: int,
) -> str:
    """Project context, one repo map, then an optional previous-attempt note."""

    block = attach_repo_map(
        project_text,
        repo_map,
        context_tokens=context_tokens,
        generation_tokens=generation_tokens,
    )
    prior_text = prior.strip()
    if not prior_text:
        return block if block.endswith("\n") else block + "\n"
    extra_head = (
        "\n\nPrevious attempt\n\n"
        "An earlier attempt on a fresh branch failed. Tests are still the judge. "
        "Do not repeat an approach that already failed these checks.\n\n"
    )
    combined = block.rstrip() + extra_head + prior_text + "\n"
    if context_fits_budget(combined, context_tokens=context_tokens, generation_tokens=generation_tokens):
        return combined
    overhead = estimate_tokens(
        [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": block.rstrip() + extra_head},
        ]
    )
    room = prompt_budget(context_tokens, generation_tokens) - overhead
    keep = max(0, room * 4)
    note = "\n[previous attempt truncated]\n"
    clipped = prior_text[: max(0, keep - len(note))].rstrip() + note
    return block.rstrip() + extra_head + clipped


def _strip_repo_map(text: str) -> str:
    marker = "<!-- asimov-repo-map -->"
    start = text.find("## Repo map\n" + marker)
    if start < 0:
        return text
    return text[:start].rstrip()


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


def _pack(
    entries: list[tuple[str, str]],
    limit: int,
    *,
    focus: str,
    markers: list[str] | tuple[str, ...] | None,
) -> ProjectContextPack:
    names = [name for name, _text in entries]
    listing = "Project context\n\nPaths (read any path that was cut):\n" + "\n".join(
        f"- {name}" for name in names
    )
    bodies = [f"## {name}\n\n{text.rstrip()}\n" for name, text in entries]
    full = listing + "\n\n" + "\n".join(bodies)
    if len(full) <= limit:
        notes = [
            ContextFileNote(name, len(text), len(text), False, [])
            for name, text in entries
        ]
        return ProjectContextPack(full.rstrip() + "\n", notes, False)
    marker_list = [item.strip().lower() for item in (markers if markers is not None else ("cheat sheet",)) if str(item).strip()]
    tokens = _focus_tokens(focus)
    overhead = len(listing) + sum(len(name) + len(_TRUNCATED.format(path=name)) + 16 for name in names)
    share = max(80, (limit - overhead) // max(1, len(entries)))
    parts = [listing, ""]
    notes: list[ContextFileNote] = []
    for name, text in entries:
        piece, truncated, omitted, kept = _fit_piece(
            name,
            text,
            share,
            tokens=tokens,
            markers=marker_list,
        )
        parts.append(piece)
        notes.append(ContextFileNote(name, len(text), kept, truncated, omitted))
    packed = "\n".join(parts).rstrip() + "\n"
    if len(packed) <= limit:
        return ProjectContextPack(packed, notes, False)
    note = "\n[truncated - read AGENTS.md for the rest]\n"
    clipped = (packed[: max(0, limit - len(note))] + note)[:limit]
    if notes:
        notes[-1].truncated = True
    return ProjectContextPack(clipped, notes, True)


def _fit_piece(
    name: str,
    text: str,
    share: int,
    *,
    tokens: set[str],
    markers: list[str],
) -> tuple[str, bool, list[str], int]:
    body = text.rstrip()
    full = f"## {name}\n\n{body}\n"
    if len(full) <= share:
        return full, False, [], len(text)
    note = _TRUNCATED.format(path=name)
    if not _has_headings(body):
        keep = max(0, share - len(note) - 8)
        piece = f"## {name}\n\n{body[:keep].rstrip()}\n{note}\n"
        return piece, True, [], keep
    header = f"## {name}\n\n"
    # Keep a line that names what was cut, so truncation is not silent.
    omitted_budget = 140
    budget = share - len(header) - len(note) - omitted_budget - 2
    selected, omitted = _select_sections(body, max(0, budget), tokens, markers)
    omitted_line = ""
    if omitted:
        listed = "Omitted sections (read the file for these): " + "; ".join(omitted)
        if len(listed) > omitted_budget:
            listed = f"Omitted sections: {len(omitted)} headings. Read the file for the rest."
        omitted_line = listed + "\n"
    piece = header + selected.rstrip() + "\n" + omitted_line + note + "\n"
    if len(piece) > share:
        keep = max(0, share - len(note) - 2)
        piece = piece[:keep].rstrip() + "\n" + note + "\n"
        if len(piece) > share:
            piece = piece[:share]
    return piece, True, omitted, len(selected)


def _has_headings(text: str) -> bool:
    return any(_HEADING.match(line) for line in text.splitlines())


def _split_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines():
        found = _HEADING.match(line)
        if found:
            if buf or heading:
                sections.append((heading, "\n".join(buf).rstrip() + "\n"))
            heading = found.group(2).strip()
            buf = [line]
            continue
        buf.append(line)
    if buf or heading:
        sections.append((heading, "\n".join(buf).rstrip() + "\n"))
    return sections


def _select_sections(
    text: str,
    budget: int,
    tokens: set[str],
    markers: list[str],
) -> tuple[str, list[str]]:
    sections = _split_sections(text)
    if budget <= 0 or not sections:
        return "", [heading for heading, _body in sections if heading]
    ranked = sorted(
        range(len(sections)),
        key=lambda index: (
            -_section_score(index, sections[index][0], sections[index][1], tokens, markers),
            index,
        ),
    )
    picked: dict[int, str] = {}
    used = 0
    for index in ranked:
        if used >= budget:
            break
        chunk = sections[index][1]
        if not chunk.endswith("\n"):
            chunk += "\n"
        score = _section_score(index, sections[index][0], sections[index][1], tokens, markers)
        if used + len(chunk) <= budget:
            picked[index] = chunk
            used += len(chunk)
            continue
        if score >= 80 and budget - used >= 80:
            remain = budget - used
            picked[index] = chunk[:remain].rstrip() + "\n"
            used = budget
            break
    if not picked:
        return text[:budget], [heading for heading, _body in sections if heading]
    omitted = [sections[index][0] for index in range(len(sections)) if index not in picked and sections[index][0]]
    ordered = "".join(picked[index] for index in range(len(sections)) if index in picked)
    return ordered, omitted


def _section_score(index: int, heading: str, body: str, tokens: set[str], markers: list[str]) -> int:
    score = max(0, 8 - index)
    if index == 0 and not heading:
        score += 55
    lowered = heading.lower()
    if heading and any(marker in lowered for marker in markers):
        score += 100
    if heading and any(token in lowered for token in tokens):
        score += 80
    sample = body[:500].lower()
    if any(token in sample for token in tokens if len(token) >= 6):
        score += 35
    return score


def _focus_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    if not text:
        return tokens
    for match in re.finditer(r"`([^`]{2,80})`", text):
        token = match.group(1).strip().lower()
        if token and token not in _FOCUS_STOP:
            tokens.add(token)
    for match in _FOCUS_WORD.finditer(text):
        word = match.group(0)
        low = word.replace("\\", "/").lower()
        if low in _FOCUS_STOP:
            continue
        camel = any(char.isupper() for char in word[1:])
        if camel or "_" in word or "/" in low or "." in low:
            tokens.add(low)
        if camel:
            for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", word):
                if len(part) >= 6 and part.lower() not in _FOCUS_STOP:
                    tokens.add(part.lower())
    return tokens
