"""Guards learned from runs that explored until the turn budget and then false-passed.

The checks here are generic. Defaults mention a few common probe commands
(ilspycmd, `dotnet new`, reflection loads) because those showed up in real
logs, but the lists are patterns a profile or a task can extend. Nothing in
this module is tied to one language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Injected once a phase has spent this many reads or probes without a write.
EXPLORATION_NUDGE = "Stop exploring. Write the files now and build early."
EXPLORATION_ESCALATION = (
    "Refusing further exploration. Stop exploring. Write the files now and build early."
)
PLAN_EXPLORATION_NUDGE = (
    "Stop exploring. Write the numbered plan now and name the files you will change. "
    "Do not keep reading."
)
PLAIN_TEXT_REMINDER = (
    "Your previous reply contained tool-call markup. This reply must be plain text. "
    "Do not emit <tool_call> tags or any other tool markup. "
    "Answer from the information already in the conversation."
)

# Closed blocks first, then leftover or unclosed tags. Qwen sometimes emits
# `<tool_call>` with no body and no closing tag when the phase has no tools.
_TOOL_BLOCK = re.compile(
    r"<\s*tool_call\b[^>]*>.*?<\s*/\s*tool_call\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BARE_TAG = re.compile(
    r"<\s*/?\s*tool_call\b[^>\n]*?>?"
    r"|<\s*/?\s*function\b[^>\n]*?>"
    r"|<\s*/?\s*parameter\b[^>\n]*?>"
    r"|<\s*/?\s*tool_response\b[^>\n]*?>"
    r"|<\s*/?\s*tools\b[^>\n]*?>",
    re.IGNORECASE,
)

DEFAULT_PROBE_PATTERNS: tuple[str, ...] = (
    r"(?i)\bilspycmd\b",
    r"(?i)\bilspy\b",
    r"(?i)\bdnspy\b",
    r"(?i)\bildasm\b",
    r"(?i)\bmonodis\b",
    r"(?i)\bjavap\b",
    r"(?i)\bjadx\b",
    r"(?i)\bdotpeek\b",
    r"(?i)\breflexil\b",
    r"(?i)reflectiononlyload",
    r"(?i)\[system\.reflection",
    r"(?i)\bsystem\.reflection\.assembly\b",
    r"(?i)\bassembly\.load(?:from|file)?\b",
    r"(?i)\bdecompile\b",
)
DEFAULT_SCAFFOLD_PATTERNS: tuple[str, ...] = (
    r"(?i)\bdotnet\s+new\b",
    r"(?i)\bnpm\s+init\b",
    r"(?i)\bcargo\s+new\b",
    r"(?i)\bgo\s+mod\s+init\b",
    r"(?i)\bmvn\s+archetype:generate\b",
)
DEFAULT_SCRATCH_MARKERS: tuple[str, ...] = (
    "diag",
    "apicheck",
    "api-check",
    "_probe",
    "scratch",
    "throwaway",
)
DEFAULT_NEW_PROJECT_SUFFIXES: tuple[str, ...] = (
    ".csproj",
    ".fsproj",
    ".vbproj",
    ".sln",
)
DEFAULT_READONLY_SHELL: tuple[str, ...] = (
    r"(?i)(?:^|[;&|]\s*)(?:rg|grep|find|ls|dir|cat|head|tail|less|more|type|findstr)\b",
    r"(?i)\b(?:get-content|get-childitem|select-string|where-object)\b",
    r"(?i)\bgit\s+(?:status|diff|log|grep|show)\b",
)
DEFAULT_API_MISUSE: tuple[str, ...] = ("CS1503", "CS0214", "CS1061", "CS0117")
DEFAULT_CHEAT_MARKERS: tuple[str, ...] = ("cheat sheet",)
DEFAULT_EXPLORATION_CAP = 10
DEFAULT_PLAN_TURNS = 8
DEFAULT_IMPLEMENT_TURNS = 30

_CHANGE_VERBS = re.compile(
    r"(?i)\b(?:add|create|edit|update|change|write|modify|implement|rename|replace|introduce)\b"
)
_PATH_IN_LINE = re.compile(
    r"(?i)(?:^|[\s\"'`(])"
    r"([A-Za-z0-9_@][\w.@-]*(?:[/\\][\w.@-]+)*\.(?:py|cs|fs|vb|csproj|sln|fsproj|vbproj|"
    r"json|toml|yml|yaml|md|js|ts|tsx|jsx|go|rs|java|ps1|sh|xml|txt|razor|cshtml|"
    r"cpp|cc|cxx|h|hpp|c|props|targets))"
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


@dataclass
class PhaseGuard:
    """Per-phase knobs. Empty extra-pattern tuples mean "defaults only"."""

    exploration_cap: int = DEFAULT_EXPLORATION_CAP
    exploration_bonus_turns: int = 0
    expect_plain_text: bool = False
    plain_text_retries: int = 0
    plan_paths: tuple[str, ...] = ()
    extra_probe_patterns: tuple[str, ...] = ()
    extra_scaffold_patterns: tuple[str, ...] = ()
    extra_scratch_markers: tuple[str, ...] = ()
    extra_project_suffixes: tuple[str, ...] = ()
    cheat_sheet: str = ""
    nudge: str = ""
    plain_text_reminder: str = ""

    def nudge_text(self) -> str:
        return self.nudge.strip() or EXPLORATION_NUDGE

    def probe_patterns(self) -> tuple[str, ...]:
        return DEFAULT_PROBE_PATTERNS + self.extra_probe_patterns

    def scaffold_patterns(self) -> tuple[str, ...]:
        return DEFAULT_SCAFFOLD_PATTERNS + self.extra_scaffold_patterns

    def scratch_markers(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for item in (*DEFAULT_SCRATCH_MARKERS, *self.extra_scratch_markers):
            key = item.strip().lower()
            if key:
                seen[key] = None
        return tuple(seen)

    def project_suffixes(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for item in (*DEFAULT_NEW_PROJECT_SUFFIXES, *self.extra_project_suffixes):
            suffix = item.strip().lower()
            if not suffix:
                continue
            if not suffix.startswith("."):
                suffix = "." + suffix
            seen[suffix] = None
        return tuple(seen)


def has_leaked_tool_markup(text: str) -> bool:
    """True when a plain-text reply still contains literal tool-call tags."""

    if not text:
        return False
    return _TOOL_BLOCK.search(text) is not None or _BARE_TAG.search(text) is not None


def strip_leaked_tool_markup(text: str) -> str:
    """Remove tool-call tags and leave the surrounding prose."""

    if not text:
        return ""
    cleaned = _TOOL_BLOCK.sub("", text)
    cleaned = _BARE_TAG.sub("", cleaned)
    return cleaned


def looks_like_api_misuse(output: str, patterns: tuple[str, ...] | list[str]) -> bool:
    if not output:
        return False
    for pattern in patterns:
        token = str(pattern).strip()
        if token and token in output:
            return True
    return False


def planned_paths(plan_text: str) -> list[str]:
    """Files the plan said it would create or edit.

    A mention of AGENTS.md on a "read this" line is not a planned edit. A
    change-verb line ("Add adder.py") and a bullet that starts with a path are.
    """

    found: list[str] = []
    seen: set[str] = set()
    for raw_line in (plan_text or "").splitlines():
        paths = _paths_in(raw_line)
        if not paths:
            continue
        if _CHANGE_VERBS.search(raw_line) or _bullet_starts_with_path(raw_line, paths):
            for path in paths:
                key = _norm(path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(path.replace("\\", "/"))
    return found


def false_pass_release(
    gap: str | None,
    *,
    fix_produced_changes: bool = False,
    consecutive_noop_fixes: int = 0,
    implement_status: str = "",
) -> tuple[str, bool] | None:
    """Why a passing gate should be accepted, or None while the guard blocks.

    Returns `(reason, accept_remaining_gap)`. The second flag is true when
    the release is a fix that changed files, or the two-noop safety valve:
    the cumulative diff may still look like a gap, and that must not be
    re-imposed after review. A release because the diff itself is real is
    re-checked at the end, since review-fix can still undo the tree.

    `gap` is `implementation_gap` on the cumulative diff from the task's base
    commit, not the last round and not `HEAD`. Implement ending on
    `turn_budget` or `exploration_capped` is not a reason to keep blocking
    once that diff is real. A fix round that changed files clears the guard
    on the next passing gate. Two passing-gate fix rounds in a row that
    change no files release it anyway, so a stuck check cannot spend the
    iteration budget.
    """

    if not gap:
        note = "cumulative diff from the task base covers the planned files"
        if implement_status in {"turn_budget", "exploration_capped"}:
            note += (
                f"; implement ended on {implement_status}, which does not block "
                "once that diff is real"
            )
        return note, False
    if fix_produced_changes:
        return "a fix round produced real file changes and the gate passed", True
    if consecutive_noop_fixes >= 2:
        return (
            "two consecutive fix rounds made no file changes and the gate passed; "
            f"stopping the false-pass loop ({gap})",
            True,
        )
    return None


def implementation_gap(plan_text: str, diff_text: str) -> str | None:
    """Why a green test run is not success, or None when the diff is real.

    An empty diff is always a gap. When the plan names files, a diff that
    does not substantively touch any of them is near-empty relative to that
    plan. A one-line edit of a named file is enough: that is a real change,
    not a near-empty one. The caller passes the cumulative diff from the
    task's base commit.
    """

    changed = diff_changed_files(diff_text)
    if not changed:
        return "implement produced an empty diff"
    planned = planned_paths(plan_text)
    if not planned:
        return None
    touched = [path for path in planned if _path_touched(path, changed)]
    if not touched:
        listed = ", ".join(planned[:8])
        return f"diff is empty relative to the files the plan named ({listed})"
    if not _substantive_change(diff_text, touched):
        listed = ", ".join(touched[:8])
        return f"diff is near-empty relative to the files the plan named ({listed})"
    return None


def diff_changed_files(diff_text: str) -> set[str]:
    if not diff_text or not diff_text.strip():
        return set()
    if "diff --git " not in diff_text and "Untracked files" not in diff_text:
        return set()
    names = set(re.findall(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.M))
    return {name.strip() for name in names if name.strip()}


def missing_must_exist(project: Path, items: list[str], test_output: str) -> list[str]:
    """Paths that are not on disk and names that are not in the test output.

    Each entry passes when it exists in the project or its text appears in
    the test output. A path satisfies "exists"; a test name satisfies
    "appears in the output".
    """

    missing: list[str] = []
    output = test_output or ""
    root = Path(project)
    for raw in items:
        item = str(raw).strip().strip('"').strip("'")
        if not item:
            continue
        rel = item.replace("\\", "/")
        candidate = root / rel
        if candidate.exists():
            continue
        if item in output or rel in output:
            continue
        missing.append(rel)
    return missing


def is_exploration_call(
    name: str,
    arguments: dict | None,
    *,
    probe_patterns: tuple[str, ...] | list[str] = (),
    scaffold_patterns: tuple[str, ...] | list[str] = (),
) -> bool:
    """Read-only or probing calls. A write, an edit, or a build is not one."""

    if name in {"read", "list_files"}:
        return True
    if name in {"write", "edit"}:
        return False
    if name != "shell":
        return False
    command = str((arguments or {}).get("command") or "")
    probes = tuple(probe_patterns) or DEFAULT_PROBE_PATTERNS
    scaffolds = tuple(scaffold_patterns) or DEFAULT_SCAFFOLD_PATTERNS
    if _matches_any(command, probes) or _matches_any(command, scaffolds):
        return True
    return _matches_any(command, DEFAULT_READONLY_SHELL)


def tool_block_reason(
    name: str,
    arguments: dict | None,
    *,
    root: Path,
    plan_paths: tuple[str, ...] | list[str],
    probe_patterns: tuple[str, ...] | list[str] = (),
    scaffold_patterns: tuple[str, ...] | list[str] = (),
    scratch_markers: tuple[str, ...] | list[str] = (),
    project_suffixes: tuple[str, ...] | list[str] = (),
) -> str | None:
    """Refuse probe commands, new project files, and scratch dirs outside the plan."""

    args = arguments or {}
    probes = tuple(probe_patterns) or DEFAULT_PROBE_PATTERNS
    scaffolds = tuple(scaffold_patterns) or DEFAULT_SCAFFOLD_PATTERNS
    markers = tuple(item.lower() for item in (scratch_markers or DEFAULT_SCRATCH_MARKERS))
    suffixes = tuple(item.lower() for item in (project_suffixes or DEFAULT_NEW_PROJECT_SUFFIXES))
    if name == "shell":
        command = str(args.get("command") or "")
        if _matches_any(command, probes):
            return (
                "command blocked: decompiling or reflection-probing is refused. "
                "Use the project cheat sheet and the files already in the repo. "
                f"Command was: {command[:180]}"
            )
        if _matches_any(command, scaffolds):
            return (
                "command blocked: creating a new project or diagnostic scaffold is refused. "
                "Write the files the plan names. Do not run dotnet new, npm init, cargo new, "
                f"or similar. Command was: {command[:180]}"
            )
        scratch = _scratch_token(command, markers)
        if scratch and not _command_allowed_by_plan(command, plan_paths, markers):
            return (
                f"command blocked: scratch directory {scratch!r} is outside the plan. "
                "Do not create diag, ApiCheck, or other probe projects."
            )
        return None
    if name not in {"write", "edit"}:
        return None
    raw_path = str(args.get("path") or "")
    if not raw_path.strip():
        return None
    rel = raw_path.replace("\\", "/").lstrip("./")
    if _is_scratch_path(rel, markers) and not _path_in_plan(rel, plan_paths):
        return (
            f"refusing to create {rel}: that directory is a scratch probe outside the plan. "
            "Write the files the plan names instead."
        )
    suffix = Path(rel).suffix.lower()
    if suffix in suffixes and not _path_in_plan(rel, plan_paths):
        if not (Path(root) / rel).is_file():
            return (
                f"refusing to create a new project file {rel} because it is not in the plan. "
                "Do not add a .csproj, .sln, or similar scaffold unless the plan names it."
            )
    return None


def extract_cheat_sheets(text: str, markers: tuple[str, ...] | list[str]) -> str:
    """Verbatim sections whose heading contains a marker. Marker match is case-insensitive."""

    needles = [item.strip().lower() for item in markers if str(item).strip()]
    if not text or not needles:
        return ""
    lines = text.splitlines()
    chunks: list[str] = []
    index = 0
    while index < len(lines):
        found = _atx(lines[index])
        if found and any(needle in found[1].lower() for needle in needles):
            level = found[0]
            start = index
            index += 1
            while index < len(lines):
                nxt = _atx(lines[index])
                if nxt and nxt[0] <= level:
                    break
                index += 1
            chunk = "\n".join(lines[start:index]).strip()
            if chunk:
                chunks.append(chunk)
            continue
        index += 1
    return "\n\n".join(chunks)


def collect_cheat_sheets(
    project: Path,
    context_paths: list[str],
    markers: tuple[str, ...] | list[str],
) -> str:
    """Full cheat-sheet sections from AGENTS.md and the task's context files.

    This reads the files themselves. It does not use the truncated Project
    context block, which is how a long sheet used to disappear after 10k chars.
    """

    root = Path(project)
    chunks: list[str] = []
    seen: set[str] = set()
    ordered = ["AGENTS.md", *context_paths]
    for raw in ordered:
        rel = str(raw).replace("\\", "/").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        path = root / rel
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
        sheet = extract_cheat_sheets(text, markers)
        if sheet:
            chunks.append(sheet)
    return "\n\n".join(chunks).strip()


def resolve_turn_budget(explicit: int, default: int, task_value: int | None, profile_value: int) -> int:
    """Caller overrides win. Otherwise the task front matter, then the profile.

    Tests pass a small explicit budget. A real overnight run leaves the
    function default in place, so front matter and the profile apply.
    """

    if explicit != default:
        return max(1, int(explicit))
    if task_value is not None:
        return max(1, int(task_value))
    if profile_value:
        return max(1, int(profile_value))
    return max(1, int(explicit))


def resolve_optional_int(task_value: int | None, profile_value: int) -> int:
    if task_value is not None:
        return int(task_value)
    return int(profile_value)


def _paths_in(line: str) -> list[str]:
    paths: list[str] = []
    for match in _PATH_IN_LINE.finditer(line):
        path = match.group(1).strip().strip("`").strip("'").strip('"')
        if "://" in path or path.startswith(".."):
            continue
        paths.append(path.replace("\\", "/"))
    return paths


def _bullet_starts_with_path(line: str, paths: list[str]) -> bool:
    stripped = line.strip()
    body = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", stripped).strip()
    body = body.strip("`")
    for path in paths:
        norm = path.replace("\\", "/")
        if body == norm or body.startswith(norm + " ") or body.startswith(norm + "—") or body.startswith(norm + " -"):
            return True
    return False


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").lower()


def _path_touched(planned: str, changed: set[str]) -> bool:
    want = _norm(planned)
    for name in changed:
        have = _norm(name)
        if have == want or have.endswith("/" + want) or want.endswith("/" + have):
            return True
    return False


def _path_in_plan(rel: str, plan_paths: tuple[str, ...] | list[str]) -> bool:
    return _path_touched(rel, set(plan_paths))


def _substantive_change(diff_text: str, touched: list[str]) -> bool:
    chunks = re.split(r"(?=^diff --git )", diff_text, flags=re.M)
    for chunk in chunks:
        match = re.search(r"^diff --git a/.+ b/(.+)$", chunk, flags=re.M)
        if not match:
            continue
        if not _path_touched(match.group(1).strip(), set(touched)):
            continue
        for line in chunk.splitlines():
            if line.startswith(("+++", "---", "diff ", "index ", "new file", "deleted file")):
                continue
            if line[:1] in {"+", "-"} and line[1:].strip():
                return True
    return False


def _matches_any(command: str, patterns: tuple[str, ...] | list[str]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, command):
                return True
        except re.error:
            if pattern and pattern in command:
                return True
    return False


def _is_scratch_path(rel: str, markers: tuple[str, ...]) -> bool:
    parts = [part.lower() for part in rel.replace("\\", "/").split("/") if part and part not in {".", ".."}]
    return any(part in markers for part in parts)


def _scratch_token(command: str, markers: tuple[str, ...]) -> str:
    """A scratch directory name used with a command that creates a directory.

    `dotnet new` and the other scaffolds are refused separately. This catches
    `mkdir diag` and `New-Item ApiCheck` when that directory is not in the plan.
    """

    if not re.search(r"(?i)\b(?:mkdir|new-item|ni|md)\b", command):
        return ""
    lowered = command.lower().replace("\\", "/")
    for marker in markers:
        if re.search(rf"(^|[\s/._-]){re.escape(marker)}([/\s._-]|$)", lowered):
            return marker
    return ""


def _command_allowed_by_plan(
    command: str,
    plan_paths: tuple[str, ...] | list[str],
    markers: tuple[str, ...],
) -> bool:
    """True when the scratch directory the command creates is a planned path."""

    lowered = command.lower().replace("\\", "/")
    for planned in plan_paths:
        if not _is_scratch_path(planned, markers):
            continue
        for part in _norm(planned).split("/"):
            if part in markers and re.search(rf"(^|[\s/._-]){re.escape(part)}([/\s._-]|$)", lowered):
                return True
    return False


def _atx(line: str) -> tuple[int, str] | None:
    match = _HEADING.match(line.strip())
    if not match:
        return None
    return len(match.group(1)), match.group(2).strip()
