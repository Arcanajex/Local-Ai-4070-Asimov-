"""Split a plan into small checked steps.

The breakdown is a plain-text reply. A step counts only when it has a goal
and a verify command, so a numbered plan that never learned this shape falls
back to one implement phase. One step is valid. The list is capped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_MAX_STEPS = 6
DEFAULT_STEP_TURNS = 12
DEFAULT_STEP_REPLANS = 1
MAX_STEPS_CAP = 8
MAX_REPLANS_CAP = 3
# Two implement-and-fix attempts, then replan. Each attempt gets this many
# fix rounds after the implement phase before it counts as a failure.
STEP_ATTEMPTS = 2
STEP_FIX_ROUNDS = 2

_HEADER = re.compile(
    r"^(?:#{1,6}[ \t]*)?(?:step[ \t]+(\d+)\b|(\d+))[.):\-]?[ \t]*(.*)$",
    re.IGNORECASE,
)
_FIELD = re.compile(r"^(goal|files|verify)[ \t]*:[ \t]*(.*)$", re.IGNORECASE)
_FENCE_LANG = (
    r"powershell|pwsh|ps1|bash|sh|shell|zsh|cmd|console|text|plaintext"
)
_FENCE_LANG_LINE = re.compile(rf"(?i)^(?:{_FENCE_LANG})$")
_INLINE_FENCE = re.compile(
    rf"(?i)^(`{{3,}})[ \t]*(?:(?:{_FENCE_LANG})[ \t]+)?(.*?)[ \t]*\1[ \t]*$"
)
_QUOTE_PAIRS = {"'": "'", '"': '"', "\u201c": "\u201d", "\u2018": "\u2019"}
_EMPTY_FILES = {"", "none", "n/a", "na", "-", "(none)", "no files", "nothing", "n/a."}
# The breakdown prompt shows this shape. An echo of the sample is not a step.
_PLACEHOLDER_GOALS = {
    "...",
    "…",
    "what this step makes true",
    "what is still missing",
}
_PLACEHOLDER_VERIFY = {
    "...",
    "…",
    "the command",
    "the one command for this step",
}


@dataclass
class Step:
    index: int
    goal: str
    files: list[str]
    verify: str


@dataclass
class StepSettings:
    enabled: bool
    max_steps: int
    step_turns: int
    replans: int


def resolve_step_settings(
    task_enabled: bool | None,
    task_max_steps: int | None,
    task_step_turns: int | None,
    task_replans: int | None,
    *,
    profile_enabled: bool = True,
    profile_max_steps: int = DEFAULT_MAX_STEPS,
    profile_step_turns: int = DEFAULT_STEP_TURNS,
    profile_replans: int = DEFAULT_STEP_REPLANS,
) -> StepSettings:
    """Task front matter wins, then the profile. Default on, 6 steps, 12 turns, 1 replan.

    `apply_project_defaults` copies AGENTS.md onto the task first, so this
    function only has to prefer the task over the profile.
    """

    enabled = profile_enabled if task_enabled is None else bool(task_enabled)
    max_steps = profile_max_steps if task_max_steps is None else int(task_max_steps)
    step_turns = profile_step_turns if task_step_turns is None else int(task_step_turns)
    replans = profile_replans if task_replans is None else int(task_replans)
    if max_steps <= 0:
        max_steps = DEFAULT_MAX_STEPS
    if step_turns <= 0:
        step_turns = DEFAULT_STEP_TURNS
    return StepSettings(
        enabled=enabled,
        max_steps=max(1, min(int(max_steps), MAX_STEPS_CAP)),
        step_turns=max(1, int(step_turns)),
        replans=max(0, min(int(replans), MAX_REPLANS_CAP)),
    )


def parse_steps(text: str, *, max_steps: int = DEFAULT_MAX_STEPS, index_start: int = 1) -> list[Step]:
    """Steps with a goal and a verify command, capped and renumbered.

    A numbered line with no verify command is ignored. That is how an ordinary
    plan stays on the single implement phase.
    """

    cap = max(1, int(max_steps))
    start = max(1, int(index_start))
    found = _extract(text or "")
    if len(found) > cap:
        found = found[:cap]
    steps: list[Step] = []
    for offset, (goal, files, verify) in enumerate(found):
        steps.append(Step(index=start + offset, goal=goal, files=files, verify=verify))
    return steps


def format_step_list(steps: list[Step], *, done: set[int], current: int | None) -> str:
    """One line per step, marked done, current, or pending."""

    lines: list[str] = []
    for step in steps:
        if step.index in done:
            mark = "done"
        elif step.index == current:
            mark = "current"
        else:
            mark = "pending"
        files = ", ".join(step.files) if step.files else "none"
        lines.append(
            f"- [{mark}] {step.index}. {step.goal} (files: {files}; verify: {step.verify})"
        )
    return "\n".join(lines)


def format_steps_markdown(steps: list[Step]) -> str:
    """The shape the breakdown prompt asks for, so the plan file can be re-parsed."""

    blocks: list[str] = []
    for step in steps:
        files = ", ".join(step.files) if step.files else "none"
        blocks.append(
            f"### Step {step.index}\nGoal: {step.goal}\nFiles: {files}\nVerify: {step.verify}"
        )
    return "\n\n".join(blocks)


def commands_match(left: str, right: str) -> bool:
    """True when two shell commands are the same aside from wrapping and space.

    Both sides are normalised first, so a verify command wrapped in Markdown
    backticks or quotes still matches the task test command.
    """

    normalized = " ".join(normalize_verify_command(left).split())
    other = " ".join(normalize_verify_command(right).split())
    return bool(normalized) and normalized == other


def normalize_verify_command(text: str) -> str:
    """Strip Markdown and quotes around a step verify command.

    Fenced blocks (``` and ```powershell) become the body. Several command
    lines become one `&&` chain, which the runner splits for PowerShell 5.1.
    A command that is already plain text is returned unchanged.
    """

    value = (text or "").replace("\r\n", "\n").strip()
    if not value:
        return ""
    for _ in range(6):
        unfenced = _unwrap_fence(value)
        if unfenced is not None and unfenced.strip() != value:
            value = unfenced.strip()
            continue
        unwrapped = _unwrap_wrapping(value)
        if unwrapped is not None and unwrapped.strip() != value:
            value = unwrapped.strip()
            continue
        break
    lines: list[str] = []
    for raw in value.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or re.fullmatch(r"`{3,}", line):
            continue
        if lines and lines[-1].endswith(("\\", "`")):
            lines[-1] = lines[-1][:-1].rstrip() + " " + line
            continue
        lines.append(line)
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    return " && ".join(lines)


def _extract(text: str) -> list[tuple[str, list[str], str]]:
    lines = text.replace("\r\n", "\n").splitlines()
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADER.match(line.strip())
        if not match:
            continue
        headers.append((index, (match.group(3) or "").strip()))
    found: list[tuple[str, list[str], str]] = []
    if not headers:
        inline = _from_fields(text, "")
        if inline is not None:
            found.append(inline)
        return found
    for position, (start, remainder) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        block = "\n".join(lines[start:end])
        parsed = _from_fields(block, remainder)
        if parsed is not None:
            found.append(parsed)
    return found


def _from_fields(block: str, remainder: str) -> tuple[str, list[str], str] | None:
    fields = _read_fields(block)
    verify = normalize_verify_command(fields.get("verify", ""))
    goal = fields.get("goal", "").strip() or _goal_from_remainder(remainder, has_verify=bool(verify))
    if not goal or not verify:
        return None
    if goal.lower() in _PLACEHOLDER_GOALS or verify.lower() in _PLACEHOLDER_VERIFY:
        return None
    if len(goal) > 500:
        goal = goal[:500].rstrip()
    return goal, _parse_files(fields.get("files", "")), verify


def _goal_from_remainder(remainder: str, *, has_verify: bool) -> str:
    """Goal text written on the step heading, when there is no Goal: line.

    A heading with no verify command is a normal plan line, not a step.
    """

    rest = remainder.strip()
    if not rest or not has_verify:
        return ""
    if "|" in rest:
        for part in rest.split("|"):
            piece = part.strip()
            lower = piece.lower()
            if lower.startswith("goal:"):
                return piece.split(":", 1)[1].strip()
            if lower.startswith("files:") or lower.startswith("verify:"):
                continue
            if piece:
                return piece
        return ""
    if rest.lower().startswith("goal:"):
        rest = rest.split(":", 1)[1].strip()
    if "files:" in rest.lower() or "verify:" in rest.lower():
        return ""
    return rest


def _read_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    lines = block.replace("\r\n", "\n").splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip().lstrip("-").strip()
        labels = re.findall(r"(?i)\b(?:goal|files|verify)\s*:", stripped)
        if len(labels) >= 2 and "|" in stripped:
            for part in stripped.split("|"):
                match = _FIELD.match(part.strip())
                if match:
                    fields[match.group(1).lower()] = match.group(2).strip()
            index += 1
            continue
        match = _FIELD.match(stripped)
        if not match:
            index += 1
            continue
        name = match.group(1).lower()
        value = match.group(2).strip()
        if name == "verify":
            value, index = _read_verify_value(value, lines, index)
        fields[name] = value
        index += 1
    return fields


def _read_verify_value(value: str, lines: list[str], index: int) -> tuple[str, int]:
    """Keep a fenced verify command that continues under the Verify: line."""

    if not _verify_continues(value, lines, index):
        return value, index
    chunks = [value]
    cursor = index
    while cursor + 1 < len(lines):
        nxt = lines[cursor + 1]
        stripped = nxt.strip()
        bare = stripped.lstrip("-").strip()
        closed = _fence_balanced("\n".join(chunks))
        if closed and (not stripped or _FIELD.match(bare) or _HEADER.match(stripped)):
            break
        if closed and not stripped.startswith("```"):
            break
        chunks.append(nxt)
        cursor += 1
        if _fence_balanced("\n".join(chunks)) and re.fullmatch(r"`{3,}", stripped):
            break
    return "\n".join(chunks).strip(), cursor


def _fence_balanced(text: str) -> bool:
    count = text.count("```")
    return count > 0 and count % 2 == 0


def _verify_continues(value: str, lines: list[str], index: int) -> bool:
    if value.count("```") % 2 == 1:
        return True
    if index + 1 >= len(lines):
        return False
    return lines[index + 1].strip().startswith("```")


def _unwrap_fence(value: str) -> str | None:
    text = value.strip()
    if not text.startswith("```"):
        return None
    lines = text.split("\n")
    if len(lines) == 1:
        match = _INLINE_FENCE.match(text)
        if not match:
            return None
        return match.group(2).strip()
    close_at = None
    for cursor, line in enumerate(lines[1:], start=1):
        if re.fullmatch(r"`{3,}", line.strip()):
            close_at = cursor
            break
    if close_at is None:
        body_lines = lines[1:]
    else:
        body_lines = lines[1:close_at]
    info = re.sub(r"^`{3,}[ \t]*", "", lines[0].strip())
    if _FENCE_LANG_LINE.match(info):
        return "\n".join(body_lines).strip()
    inline = re.match(rf"(?i)^(?:{_FENCE_LANG})[ \t]+(.+)$", info)
    if inline:
        extra = inline.group(1).strip()
        body = "\n".join(body_lines).strip()
        return f"{extra}\n{body}".strip() if body else extra
    if info:
        body = "\n".join(body_lines).strip()
        return f"{info}\n{body}".strip() if body else info
    return "\n".join(body_lines).strip()


def _unwrap_wrapping(value: str) -> str | None:
    text = value.strip()
    if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        return text[1:-1]
    if "\n" in text or len(text) < 2 or not text.startswith("`") or not text.endswith("`"):
        return None
    marks = 0
    while marks < len(text) and text[marks] == "`":
        marks += 1
    if marks >= 3 or marks >= len(text) or not text.endswith("`" * marks):
        return None
    inner = text[marks:-marks]
    if "`" in inner:
        return None
    return inner


def _parse_files(value: str) -> list[str]:
    text = (value or "").strip().strip("`")
    if text.lower() in _EMPTY_FILES:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"\s*(?:,|;|\band\b)\s*", text):
        token = chunk.strip().strip("`").strip("'").strip('"').strip()
        token = token.replace("\\", "/").rstrip(".,;")
        token = token.lstrip("./")
        if not token or token.lower() in _EMPTY_FILES:
            continue
        if not _looks_like_path(token):
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        files.append(token)
    return files


def _looks_like_path(token: str) -> bool:
    if not token or " " in token or "://" in token:
        return False
    if "/" in token:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", token))
