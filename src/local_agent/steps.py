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
    """True when two shell commands are the same aside from surrounding space."""

    return " ".join((left or "").split()) == " ".join((right or "").split()) and bool((left or "").strip())


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
    verify = fields.get("verify", "").strip()
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
    for raw in block.splitlines():
        stripped = raw.strip().lstrip("-").strip()
        labels = re.findall(r"(?i)\b(?:goal|files|verify)\s*:", stripped)
        if len(labels) >= 2 and "|" in stripped:
            for part in stripped.split("|"):
                match = _FIELD.match(part.strip())
                if match:
                    fields[match.group(1).lower()] = match.group(2).strip()
            continue
        match = _FIELD.match(stripped)
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
    return fields


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
