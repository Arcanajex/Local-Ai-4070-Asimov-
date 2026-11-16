"""Review evidence and verify-only verdicts.

The review phase has no tools. A local model will still vote `fix` and ask
to run a command or confirm a symbol the harness already checked, and it
will claim a file is missing when that file is in the prompt. This module
puts the harness results in the prompt and, when every open item is only a
request to re-check a green result, treats the vote as a pass.

A concrete defect still goes to review-fix. A claim that a file or symbol
is missing is dropped when the tree contains it. An acceptance line marked
not met or unverified is a verify-only item when its only stated reason is
that the harness did not run or execute the check. Any other not-met line,
including an ambiguous one, still rejects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from local_agent.guardrails import missing_must_exist
from local_agent.taskcheck import extract_section, review_problems

EXCERPT_PER_FILE = 8_000
EXCERPT_TOTAL = 24_000
TAIL_LINES = 40
TAIL_CHARS = 2_000
_CONTENT_FILE_CAP = 800
_CONTENT_BYTES = 200_000

_SKIP_DIRS = {
    ".git",
    ".agent",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "bin",
    "obj",
}
# Docs often name a symbol the code does not have yet. A mention only in
# markdown is not evidence the implementation contains it. Changed files are
# still read whatever their suffix is.
_CODE_SUFFIXES = {
    ".py",
    ".cs",
    ".fs",
    ".vb",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".xml",
    ".csproj",
    ".fsproj",
    ".vbproj",
    ".sln",
    ".java",
    ".go",
    ".rs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".ini",
    ".cfg",
    ".ps1",
    ".sh",
    ".rb",
    ".php",
    ".swift",
    ".kt",
}
_GENERIC_TARGETS = {
    "file",
    "files",
    "code",
    "test",
    "tests",
    "build",
    "symbol",
    "symbols",
    "class",
    "classes",
    "method",
    "methods",
    "function",
    "functions",
    "import",
    "imports",
    "using",
    "missing",
    "the",
    "this",
    "that",
    "output",
    "result",
    "check",
    "line",
    "change",
    "diff",
    "task",
    "implementation",
    "feature",
    "module",
    "project",
    "tree",
    "source",
    "directive",
    "statement",
    "reference",
    "package",
    "type",
    "name",
    "item",
    "items",
}
_BLANK_ITEMS = {
    "none",
    "no",
    "n/a",
    "na",
    "nothing",
    "none found",
    "no conflict",
    "no conflicts",
    "no conflicts found",
    "no issues",
    "no issue",
    "no risks",
    "no risk",
    "no bugs",
    "no bug",
    "no findings",
    "no finding",
    "no files",
    "no file",
    "no problems",
    "no problem",
    "no leftover bugs",
    "nothing to report",
    "lgtm",
}
_OPEN_SECTIONS = {"Findings", "Risks", "Open questions"}
_SCOPE_SECTIONS = {
    "Conflicts with AGENTS.md or decisions",
    "Out-of-scope files touched",
}
_FINDING_SECTIONS = (
    "Findings",
    "Risks",
    "Open questions",
    "Tests",
    "Correctness",
    "Acceptance criteria",
    "Conflicts with AGENTS.md or decisions",
    "Out-of-scope files touched",
)

# A vote other than `accept` that means the same thing as `fix`.
_FIX_VERDICT = re.compile(
    r"(?im)^[ \t>*-]*verdict\s*:\s*("
    r"fix|reject(?:ed)?|"
    r"changes?\s+requested|request\s+changes|"
    r"needs?\s+(?:changes|work|fixes?)|"
    r"revise|revision\s+requested"
    r")\b"
)
_MISSING_LANGUAGE = re.compile(
    r"(?i)("
    r"\bmissing\b|\bnot found\b|\bdoes not exist\b|\bdoesn't exist\b|"
    r"\bdo not exist\b|\bnot present\b|\bno such file\b|"
    r"\bcannot find\b|\bcan't find\b|\bcould not find\b|"
    r"\bnot defined\b|\bwas not added\b|\bwere not added\b|"
    r"\bis absent\b|\bare absent\b|"
    r"\bno file\b|\bno symbol\b|\bno class\b|"
    r"\bnot in the (?:file|tree|diff|code|project)\b"
    r")"
)
_VERIFY = re.compile(
    r"(?i)("
    r"\[verify-only\]|\[verify only\]|"
    r"\bneed(?:s|ed)? to (?:re-?)?(?:run|verify|check|confirm|validate|test)\b|"
    r"\b(?:please|should|must|can you|can)\s+(?:re-?)?(?:run|verify|check|confirm|validate)\b|"
    r"\b(?:re-?)?verify\b|\b(?:re-?)?confirm\b|"
    r"\b(?:re-?)?check that\b|"
    r"\brun (?:the )?(?:tests?|build|smoke|command|app)\b|"
    r"\bnot (?:yet )?verified\b|"
    r"\b(?:was|were|is|are) not verified\b|"
    r"\bcould not (?:run|verify|check|confirm|execute)\b|"
    r"\bunable to (?:run|verify|check|confirm|execute)\b|"
    r"\bhaven't (?:run|verified|checked|confirmed)\b|"
    r"\bhas not been (?:run|verified|checked|confirmed|executed)\b|"
    r"\bhave not been (?:run|verified|checked|confirmed)\b|"
    r"\bneeds? (?:verification|confirmation)\b|"
    r"\bneeds? to be (?:verified|confirmed|checked|run|tested)\b|"
    r"\bshould be (?:verified|confirmed|checked|run|tested)\b|"
    r"\brecommend(?:ed)? (?:running|verifying|checking|confirming)\b|"
    r"\bdid not (?:run|execute|verify|check|confirm)\b|"
    r"\bwithout (?:running|verifying|checking|confirming)\b"
    r")"
)
_DEFECT = re.compile(
    r"(?i)("
    r"\[defect\]|"
    r"\bwrong\b|\bincorrect\b|\bbug\b|\bbroken\b|"
    r"\bfails?\b|\bfailing\b|\bcrash(?:es|ed)?\b|\bexception\b|\bthrows?\b|"
    r"\binstead of\b|\bshould return\b|\breturns? \d|\boff[- ]by[- ]one\b|"
    r"\bnot met\b|\bregression\b|"
    r"\bnever (?:runs|opens|works|returns|stops)\b|"
    r"\bhangs?\b|\binfinite\b|"
    r"\bdoes not\b|\bdoesn't\b|\bdo not\b|\bdon't\b|"
    r"\bmissing\b|\bnot found\b|\bdoes not exist\b|\bdoesn't exist\b|"
    r"\bno such file\b|\bnot defined\b|"
    r"\bnull\b"
    r")"
)
_NARRATION = re.compile(
    r"(?i)^(?:the|this|it|tests?|build|pytest|implementation|change|diff|code|harness)\b"
)
_POSITIVE = re.compile(
    r"(?i)\b(?:passed|passes|looks correct|looks fine|matches the task|implemented)\b"
)


@dataclass
class VerifyCommandResult:
    """One acceptance command the harness ran before review."""

    command: str
    ok: bool
    exit_code: int | None
    output: str


@dataclass
class Finding:
    text: str
    section: str
    kind: str  # "defect" or "verify-only"


@dataclass
class ReviewRuling:
    """What the runner should do with one review reply."""

    action: str  # "pass" or "fix"
    problems: list[str] = field(default_factory=list)
    verify_items: list[str] = field(default_factory=list)
    contradicted: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)


def is_fix_verdict(text: str) -> bool:
    """True when the review votes fix, reject, or another equivalent."""

    return _FIX_VERDICT.search(text or "") is not None


def parse_test_counts(output: str) -> str | None:
    """A short `N passed, M failed` summary, or None when the output has no counts.

    The last matching summary wins. Pytest, dotnet test, cargo, and xUnit
    each print the totals at the bottom.
    """

    tail = "\n".join((output or "").splitlines()[-100:])
    if not tail.strip():
        return None
    dotnet = list(
        re.finditer(
            r"Failed:\s*(\d+),\s*Passed:\s*(\d+)"
            r"(?:,\s*Skipped:\s*(\d+))?(?:,\s*Total:\s*(\d+))?",
            tail,
            re.I,
        )
    )
    if dotnet:
        match = dotnet[-1]
        return _format_counts(
            passed=match.group(2),
            failed=match.group(1),
            skipped=match.group(3),
            total=match.group(4),
        )
    xunit = list(
        re.finditer(
            r"Total tests:\s*(\d+)\.\s*Passed:\s*(\d+)\.\s*Failed:\s*(\d+)"
            r"(?:\.\s*Skipped:\s*(\d+))?",
            tail,
            re.I,
        )
    )
    if xunit:
        match = xunit[-1]
        return _format_counts(
            passed=match.group(2),
            failed=match.group(3),
            skipped=match.group(4),
            total=match.group(1),
        )
    cargo = list(re.finditer(r"(\d+)\s+passed;\s*(\d+)\s+failed(?:;\s*(\d+)\s+ignored)?", tail, re.I))
    if cargo:
        match = cargo[-1]
        return _format_counts(passed=match.group(1), failed=match.group(2), skipped=match.group(3))
    summary = ""
    for line in tail.splitlines():
        if re.search(r"\d+\s+(?:passed|failed|errors?)\b", line, re.I):
            summary = line
    if not summary:
        return None
    return _format_counts(
        passed=_grab_count(summary, "passed"),
        failed=_grab_count(summary, "failed"),
        errors=_grab_count(summary, "errors?"),
        skipped=_grab_count(summary, "skipped"),
    )


def summarize_test_result(output: str, passed: bool) -> str:
    state = "passed" if passed else "failed"
    counts = parse_test_counts(output)
    if counts:
        return f"{state} ({counts})"
    return state


def output_tail(text: str, *, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
    body = (text or "").strip()
    if not body:
        return "(no output)"
    kept = "\n".join(body.splitlines()[-lines:])
    if len(kept) <= chars:
        return kept
    return "…\n" + kept[-chars:]


def split_verify_output(text: str) -> tuple[str, str]:
    """Split a combined check log into the earlier part and the verify part."""

    for marker in ("\n\nVerify check passed.\n", "\n\nVerify check failed:\n"):
        if marker in (text or ""):
            head, tail = text.rsplit(marker, 1)
            return head, tail
    return text or "", ""


def split_smoke_output(text: str) -> tuple[str, str]:
    """Split the combined check log into the test part and the smoke part."""

    for marker in ("\n\nSmoke check passed.\n", "\n\nSmoke check failed:\n"):
        if marker in (text or ""):
            head, tail = text.rsplit(marker, 1)
            return head, tail
    return text or "", ""


def must_exist_rows(
    project: Path, items: list[str], test_output: str
) -> tuple[list[tuple[str, str]], list[str]]:
    """One row per configured item, plus the names that failed."""

    missing = missing_must_exist(project, items, test_output)
    missing_set = set(missing)
    rows: list[tuple[str, str]] = []
    for raw in items:
        item = str(raw).strip()
        if not item:
            continue
        rel = item.replace("\\", "/")
        if rel in missing_set or item in missing_set:
            rows.append((item, "missing"))
            continue
        if (project / rel).exists():
            rows.append((item, "present"))
        else:
            rows.append((item, "present in test output"))
    return rows, missing


def harness_checks_green(
    *,
    tests_passed: bool,
    smoke_passed: bool | None,
    must_missing: list[str],
    verify_passed: bool | None = None,
) -> bool:
    """True when build/test, must_exist, smoke, and verify commands all passed.

    `smoke_passed is None` means the task has no smoke command, which is a
    pass. A smoke failure is `False`. `verify_passed is None` means the task
    has no verify commands. A verify failure is `False`.
    """

    if not tests_passed:
        return False
    if smoke_passed is False:
        return False
    if verify_passed is False:
        return False
    return not must_missing


def build_harness_evidence(
    project: Path,
    *,
    test_command: str,
    lint_command: str | None,
    tests_passed: bool,
    test_output: str,
    must_exist: list[str],
    smoke_passed: bool | None,
    smoke_command: str | None,
    changed_files: list[str],
    verify_results: list[VerifyCommandResult] | None = None,
) -> str:
    """The block pasted into the review prompt.

    The reviewer cannot run commands. This is the record of what the harness
    already ran, plus the text of the files that changed, so a claim that a
    symbol is missing can be checked against the tree.
    """

    body, _verify_part = split_verify_output(test_output)
    test_part, smoke_part = split_smoke_output(body)
    rows, _missing = must_exist_rows(project, must_exist, test_part)
    lines = [
        "HARNESS CHECKS (executed by the harness, not by you):",
        "These results are authoritative. Do not request running, verifying, "
        "checking, or confirming anything already covered here.",
        "Do not claim a file, import, or symbol is missing when it appears in "
        "the changed-file contents below or elsewhere in the tree.",
        "",
        f"Build/test command: {test_command or '(none)'}",
    ]
    if lint_command:
        lines.append(f"Lint command (runs only after the test command passes): {lint_command}")
    lines.extend(
        [
            f"Result: {summarize_test_result(test_part, tests_passed)}",
            "Test output tail:",
            output_tail(test_part),
            "",
            "must_exist:",
        ]
    )
    if not rows:
        lines.append("- (none configured)")
    else:
        lines.extend(f"- {item}: {status}" for item, status in rows)
    lines.append("")
    if smoke_passed is None:
        lines.append("Smoke: not run (no smoke command).")
    else:
        lines.append(f"Smoke: {'passed' if smoke_passed else 'failed'}")
        if smoke_command:
            lines.append(f"Smoke command: {smoke_command}")
        lines.append("Smoke output tail:")
        lines.append(output_tail(smoke_part))
    lines.append("")
    lines.extend(_verify_evidence_lines(verify_results))
    lines.extend(["", "Files changed since the task base:"])
    if not changed_files:
        lines.append("- (none)")
    else:
        lines.extend(f"- {name}" for name in changed_files)
    lines.extend(["", "Changed file contents (full text, or a bounded excerpt):"])
    lines.append(_file_excerpts(project, changed_files))
    return "\n".join(lines).rstrip() + "\n"


def extract_findings(text: str) -> list[Finding]:
    """Open items from the review sections, each tagged defect or verify-only.

    A heading may be its own line (`Risks:` plus bullets) or inline
    (`Risks: need to verify ObjLoader exists`). Both show up in real reviews.
    """

    found: list[Finding] = []
    seen: set[str] = set()

    def add(item: str, title: str) -> None:
        if re.match(r"(?i)^verdict\s*:", item.strip()):
            return
        kind = classify_finding(item, title)
        if kind is None:
            return
        key = re.sub(r"\s+", " ", item.strip().lower())
        if not key or key in seen:
            return
        seen.add(key)
        found.append(Finding(item.strip(), title, kind))

    body_text = text or ""
    for title in _FINDING_SECTIONS:
        body = extract_section(body_text, title)
        if not body:
            continue
        for item in _split_items(body):
            add(item, title)
    inline = re.compile(
        r"(?im)^(" + "|".join(re.escape(title) for title in _FINDING_SECTIONS) + r")\s*:\s+(\S.*?)\s*$"
    )
    for match in inline.finditer(body_text):
        for item in _split_items(match.group(2)):
            add(item, match.group(1))
    return found


_STATUS_MARK = re.compile(r"(?i)\b(?:not met|unverified)\b")
_UNRUN_REASON = re.compile(
    r"(?i)(?:"
    r"\bharness\b.{0,60}\b(?:did not|didn't|has not|have not|never|does not|doesn't)\b.{0,30}\b(?:run|ran|execute|executed)\b"
    r"|"
    r"\b(?:did not|didn't|has not|have not|was not|were not|wasn't|weren't|never)\s+(?:run|ran|execute|executed)\b"
    r"|"
    r"\bnot\s+(?:run|executed)\s+by\s+the\s+harness\b"
    r"|"
    r"\b(?:command|commands|check|checks|variant|variants)\b.{0,40}\b(?:was|were|is|are)\s+not\s+(?:run|executed)\b"
    r"|"
    r"\bnot\s+(?:been\s+)?(?:run|executed)\b"
    r")"
)
_REASON_DEFECT = re.compile(
    r"(?i)("
    r"\[defect\]|"
    r"\bwrong\b|\bincorrect\b|\bbug\b|\bbroken\b|"
    r"\bfails?\b|\bfailed\b|\bfailing\b|"
    r"\bcrash(?:es|ed|ing)?\b|\bexception\b|\bthrows?\b|"
    r"\binstead\b|\bregression\b|"
    r"\bmissing\b|\bnull\b|\berror\b|\btraceback\b|"
    r"\bhangs?\b|\binfinite\b|"
    r"\bnonzero\b|\bnon-zero\b|"
    r"\bexit code\s*[1-9]"
    r")"
)
_REASON_FILLER = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "command",
    "commands",
    "check",
    "checks",
    "variant",
    "variants",
    "harness",
    "only",
    "because",
    "so",
    "and",
    "or",
    "to",
    "of",
    "for",
    "by",
    "was",
    "were",
    "is",
    "are",
    "not",
    "run",
    "ran",
    "execute",
    "executed",
    "executing",
    "did",
    "does",
    "do",
    "two",
    "both",
    "all",
    "them",
    "they",
    "with",
    "on",
    "in",
    "from",
    "has",
    "have",
    "been",
    "be",
    "never",
    "actually",
    "yet",
    "which",
    "when",
    "after",
    "before",
    "during",
    "their",
    "there",
    "also",
    "just",
    "still",
}
_BACKTICK_SPAN = re.compile(r"`([^`\n]+)`")
_RUN_CUE = re.compile(r"(?i)\b(?:run|execute|executed|executing|exit code|exits?)\b")
_KNOWN_RUNNER = re.compile(
    r"(?i)^(?:python3?|py|pytest|dotnet|cargo|npm|npx|pnpm|yarn|node|go|pwsh|powershell|bash|sh|cmd|msbuild)(?:\.exe)?$"
)
_SHELL_BUILTIN = {"exit", "true", "false", "echo", "cd", "dir", "ls", "type", "cat"}
_BAD_SHELL = re.compile(r"[|;<>]|\$\(")


def acceptance_unrun_only(text: str) -> bool:
    """True when a not-met or unverified line only says the check was not run.

    The reason after the status marker has to say the harness, command, or
    check was not run or not executed, and it must not also describe a defect.
    An empty reason, a defect word, or any other leftover claim is ambiguous
    and returns False so the line still rejects.
    """

    raw = _strip_bullet(text)
    if not raw or _STATUS_MARK.search(raw) is None:
        return False
    match = None
    for match in _STATUS_MARK.finditer(raw):
        pass
    if match is None:
        return False
    reason = _unwrap_reason(raw[match.end() :])
    if not reason or _UNRUN_REASON.search(reason) is None:
        return False
    if _reason_has_defect(reason):
        return False
    return _reason_leftover(reason) == ""


def extract_acceptance_commands(section: str) -> list[str]:
    """Backtick commands from an Acceptance criteria section.

    A span is kept only when it looks like a command to run. A flag fragment
    such as `--frames 60`, a function call, or a bare filename is left out.
    `&&` stays in the span; the runner splits it because PowerShell 5.1 has
    no `&&`. Callers should treat this as opt-in: a guessed command is easy
    to get wrong.
    """

    found: list[str] = []
    seen: set[str] = set()
    for line in (section or "").splitlines():
        spans = _BACKTICK_SPAN.findall(line)
        if not spans:
            continue
        cue = _RUN_CUE.search(line) is not None
        for span in spans:
            command = span.strip()
            if not _reliable_command(command, cue):
                continue
            if command in seen:
                continue
            seen.add(command)
            found.append(command)
    return found


def classify_finding(text: str, section: str) -> str | None:
    """`defect`, `verify-only`, or None when the line is not an open item.

    An explicit `[defect]` tag wins. A line that both asks to verify and
    describes a concrete failure is a defect. An acceptance line marked
    `not met` or `unverified` is verify-only when its only reason is that
    the harness did not run or execute the check. Any other acceptance line
    that is not explicitly met is a defect. Narration in Correctness or Tests
    is ignored. Anything else left under Findings, Risks, or Open questions
    is a defect, so a verify-only pass cannot hide an item the heuristic did
    not recognize.
    """

    if _is_blank_item(text):
        return None
    if section == "Acceptance criteria":
        if acceptance_unrun_only(text):
            return "verify-only"
        if _STATUS_MARK.search(text):
            return "defect"
        if re.search(r"(?i)\bmet\b", text):
            return None
        return "defect"
    if section in _SCOPE_SECTIONS:
        return "defect"
    if re.search(r"(?i)\[defect\]", text):
        return "defect"
    defect = _DEFECT.search(text) is not None
    verify = _VERIFY.search(text) is not None
    if defect:
        return "defect"
    if verify:
        return "verify-only"
    if _is_narration(text):
        return None
    if section in _OPEN_SECTIONS:
        return "defect"
    return None


def contradicted_by_tree(text: str, project: Path) -> bool:
    """True when the line says something is missing and every named target exists.

    No target, or a target that is not on disk and not in a source file,
    means the claim stands. A generic word such as "tests" is not a target.
    """

    if not text or _MISSING_LANGUAGE.search(text) is None:
        return False
    targets = missing_targets(text)
    if not targets:
        return False
    return all(target_in_tree(project, target) for target in targets)


def missing_targets(text: str) -> list[str]:
    """File paths and symbols a missing-claim is about."""

    found: list[str] = []
    found.extend(re.findall(r"`([^`\n]{2,160})`", text))
    found.extend(re.findall(r"'([^'\n]{2,160})'", text))
    found.extend(re.findall(r'"([^"\n]{2,160})"', text))
    found.extend(
        re.findall(
            r"\b[\w./\\-]+\.(?:py|cs|fs|js|ts|tsx|jsx|json|md|txt|xml|csproj|sln|java|go|rs|cpp|h|hpp|vb)\b",
            text,
        )
    )
    found.extend(re.findall(r"\busing\s+[A-Za-z_][\w.]*\s*;", text))
    for pattern in (
        r"(?i)\bmissing\s+(?:the\s+)?(?:file\s+|symbol\s+|class\s+|directive\s+)?`?([A-Za-z_][\w.]*)`?",
        r"(?i)\b([A-Za-z_][\w.]*)\s+is\s+missing\b",
        r"(?i)\b([A-Za-z_][\w.]*)\s+(?:does not|doesn't)\s+exist\b",
        r"(?i)\b(?:cannot|can't|could not)\s+find\s+`?([A-Za-z_][\w.]*)`?",
        r"(?i)\b([A-Za-z_][\w.]*)\s+(?:was|were)\s+not\s+found\b",
        r"(?i)\bno\s+(?:such\s+)?(?:file|symbol|class|method|function|type)\s+`?([A-Za-z_][\w.]*)`?",
    ):
        found.extend(re.findall(pattern, text))
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in found:
        token = str(raw).strip().strip("`").strip("'").strip('"').strip()
        key = token.lower()
        if not token or key in seen or not _usable_target(token):
            continue
        seen.add(key)
        cleaned.append(token)
    return cleaned


def target_in_tree(project: Path, target: str) -> bool:
    """True when `target` is a path in the project or its text is in a source file."""

    token = target.strip().strip("`")
    if len(token) < 3:
        return False
    rel = token.replace("\\", "/").lstrip("./")
    if _looks_like_path(rel):
        # A filename has to be on disk. A mention of that name inside some
        # other file is not the file.
        candidate = project / rel
        if _within(project, candidate) and candidate.exists():
            return True
        name = Path(rel).name
        return bool(name) and _find_named(project, name)
    return _content_has(project, token)


def decide_review(
    text: str,
    project: Path,
    *,
    checks_green: bool,
    allow_verify_only_pass: bool,
    require_test_coverage: bool = False,
) -> ReviewRuling:
    """Pass, fix, or pass-by-evidence for one well-formed review.

    A real `not met` line, a conflict, a coverage gap, and any other concrete
    defect still fix. An acceptance line whose only reason is that the
    harness did not run or execute the check is verify-only, including when
    the verdict is `accept`. A `fix` or `accept` vote whose remaining items
    are all verify/run/check/confirm requests passes only when the harness
    checks are green and the flag is on. A missing-file claim that the tree
    contradicts is dropped and does not count as a defect. An ambiguous
    not-met or unverified line still rejects.
    """

    problems = review_problems(text, require_test_coverage=require_test_coverage)
    status_lines = _acceptance_status_lines(text)
    unrun_lines = [line for line in status_lines if acceptance_unrun_only(line)]
    unrun_norm = {_norm_line(line) for line in unrun_lines}
    problems = [problem for problem in problems if _norm_line(problem) not in unrun_norm]
    seen_norms = {_norm_line(problem) for problem in problems}
    for line in status_lines:
        norm = _norm_line(line)
        if norm in unrun_norm or norm in seen_norms:
            continue
        if re.search(r"(?i)\bunverified\b", line):
            problems.append(line)
            seen_norms.add(norm)
    contradicted: list[str] = []
    verify_items: list[str] = []
    defect_items: list[str] = []
    for finding in extract_findings(text):
        if _norm_line(finding.text) in unrun_norm:
            verify_items.append(finding.text)
            continue
        if contradicted_by_tree(finding.text, project):
            contradicted.append(finding.text)
            continue
        if finding.kind == "defect":
            defect_items.append(finding.text)
        elif finding.kind == "verify-only":
            verify_items.append(finding.text)
    for line in unrun_lines:
        if not any(_norm_line(line) == _norm_line(item) for item in verify_items):
            verify_items.append(line)
    contradicted = _dedupe(contradicted)
    verify_items = _dedupe(verify_items)
    defect_items = [item for item in _dedupe(defect_items) if _norm_line(item) not in unrun_norm]
    blocked = _blocking_problems(problems, contradicted)
    fix_vote = is_fix_verdict(text) or any(item == "verdict: fix" for item in problems)

    if blocked:
        reasons = list(blocked)
        if fix_vote and "verdict: fix" not in reasons:
            reasons.insert(0, "verdict: fix")
        return ReviewRuling("fix", reasons, verify_items, contradicted, _dedupe([*blocked, *defect_items]))

    if not fix_vote:
        if unrun_lines and not (allow_verify_only_pass and checks_green):
            listed = _dedupe(unrun_lines)
            return ReviewRuling("fix", listed, verify_items, contradicted, listed)
        return ReviewRuling("pass", [], verify_items, contradicted, [])

    if defect_items:
        return ReviewRuling("fix", ["verdict: fix", *defect_items], verify_items, contradicted, defect_items)

    if verify_items and not (allow_verify_only_pass and checks_green):
        return ReviewRuling("fix", ["verdict: fix"], verify_items, contradicted, [])

    if verify_items and allow_verify_only_pass and checks_green:
        return ReviewRuling("pass", [], verify_items, contradicted, [])

    if contradicted:
        return ReviewRuling("pass", [], [], contradicted, [])

    return ReviewRuling("fix", ["verdict: fix"], [], contradicted, ["verdict: fix"])


def present_review(text: str, ruling: ReviewRuling) -> str:
    """Review text for the report, with contradicted lines removed on a fix path."""

    body = text
    if ruling.action == "fix" and ruling.contradicted:
        body = _drop_lines(text, ruling.contradicted)
    notes: list[str] = []
    if ruling.contradicted:
        listed = "\n".join(f"- {item}" for item in ruling.contradicted)
        notes.append(
            "These findings were dropped because the file or symbol is in the tree. "
            f"Do not fix them:\n{listed}"
        )
    if ruling.action == "pass" and ruling.verify_items:
        listed = "\n".join(f"- {item}" for item in ruling.verify_items)
        notes.append(
            "Treated as a pass. Every open item only asked to run, verify, check, or confirm, "
            f"and the harness checks passed:\n{listed}"
        )
    if not notes:
        return body
    return body.rstrip() + "\n\nHarness note:\n" + "\n\n".join(notes) + "\n"


def _verify_evidence_lines(results: list[VerifyCommandResult] | None) -> list[str]:
    if not results:
        return ["Verify: not run (no verify commands)."]
    lines = ["Verify commands (executed by the harness):"]
    for item in results:
        state = "passed" if item.ok else "failed"
        code = "unknown" if item.exit_code is None else str(item.exit_code)
        lines.append(f"- {item.command}: {state} (exit {code})")
        lines.append("  output tail:")
        for tail_line in output_tail(item.output).splitlines():
            lines.append(f"  {tail_line}")
    return lines


def _acceptance_status_lines(text: str) -> list[str]:
    body = extract_section(text or "", "Acceptance criteria")
    lines: list[str] = []
    for raw in body.splitlines():
        line = _strip_bullet(raw)
        if line and _STATUS_MARK.search(line):
            lines.append(line)
    return lines


def _norm_line(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_bullet(text)).strip().lower()


def _strip_bullet(text: str) -> str:
    line = (text or "").strip()
    line = re.sub(r"^[-*+]\s+", "", line)
    line = re.sub(r"^\d+[.)]\s+", "", line)
    return line.strip()


def _unwrap_reason(reason: str) -> str:
    text = reason.strip().strip(".:;—- ").strip()
    if len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        depth = 0
        wraps = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    wraps = False
                    break
        if wraps and depth == 0:
            text = text[1:-1].strip()
    return text


def _reason_has_defect(reason: str) -> bool:
    scrubbed = _UNRUN_REASON.sub(" ", reason)
    return _REASON_DEFECT.search(scrubbed) is not None


def _reason_leftover(reason: str) -> str:
    text = _UNRUN_REASON.sub(" ", reason)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"--?[\w.-]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9\s]", " ", text)
    words = [
        word.lower()
        for word in text.split()
        if word.lower() not in _REASON_FILLER and not word.isdigit()
    ]
    return " ".join(words)


def _reliable_command(command: str, cue: bool) -> bool:
    if not command or len(command) > 400 or "\n" in command:
        return False
    if _BAD_SHELL.search(command):
        return False
    parts = [part.strip() for part in re.split(r"\s*&&\s*", command) if part.strip()]
    if not parts:
        return False
    return all(_reliable_step(part, cue) for part in parts)


def _reliable_step(part: str, cue: bool) -> bool:
    if part.startswith("-") or "(" in part or ")" in part:
        return False
    if part[:1] in {"'", '"'}:
        return cue or re.search(r"(?i)\.(?:exe|ps1|sh|bat|cmd)\b", part) is not None
    tokens = part.split()
    if not tokens or tokens[0].startswith("-"):
        return False
    first = tokens[0]
    known = _KNOWN_RUNNER.match(first) is not None
    script = (
        first.startswith("./")
        or first.startswith(".\\")
        or first.startswith("../")
        or re.search(r"(?i)\.(?:ps1|sh|bat|cmd|exe)$", first) is not None
    )
    has_args = len(tokens) >= 2
    if (known or script) and (cue or has_args):
        return True
    return cue and first.lower() in _SHELL_BUILTIN


def _blocking_problems(problems: list[str], contradicted: list[str]) -> list[str]:
    blocked: list[str] = []
    for problem in problems:
        if problem == "verdict: fix":
            continue
        if _mentioned_in(problem, contradicted):
            continue
        blocked.append(problem)
    return blocked


def _mentioned_in(problem: str, items: list[str]) -> bool:
    left = problem.strip()
    if not left:
        return False
    for item in items:
        right = item.strip()
        if not right:
            continue
        if left == right or (len(left) >= 12 and left in right) or (len(right) >= 12 and right in left):
            return True
    return False


def _drop_lines(text: str, items: list[str]) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        if _mentioned_in(line, items):
            continue
        kept.append(line)
    return "\n".join(kept)


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = re.sub(r"\s+", " ", item.strip().lower())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item.strip())
    return result


def _format_counts(
    *,
    passed: str | None = None,
    failed: str | None = None,
    errors: str | None = None,
    skipped: str | None = None,
    total: str | None = None,
) -> str | None:
    parts: list[str] = []
    if passed is not None:
        parts.append(f"{int(passed)} passed")
    if failed is not None:
        parts.append(f"{int(failed)} failed")
    if errors is not None:
        parts.append(f"{int(errors)} errors")
    if skipped is not None:
        parts.append(f"{int(skipped)} skipped")
    if total is not None:
        parts.append(f"{int(total)} total")
    if not parts:
        return None
    return ", ".join(parts)


def _grab_count(line: str, word: str) -> str | None:
    match = re.search(rf"(\d+)\s+{word}\b", line, re.I)
    if not match:
        return None
    return match.group(1)


def _split_items(body: str) -> list[str]:
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        parts = re.split(r"(?<=[.!?])\s+(?=(?:\[|[A-Z]))", line)
        for part in parts:
            part = part.strip()
            if part:
                items.append(part)
    return items


def _is_blank_item(text: str) -> bool:
    cleaned = text.strip().lstrip("-*").strip().rstrip(".").rstrip(":").lower()
    cleaned = re.sub(r"^\[[ xX]\]\s*", "", cleaned)
    return cleaned in _BLANK_ITEMS


def _is_narration(text: str) -> bool:
    if _VERIFY.search(text) or _DEFECT.search(text):
        return False
    stripped = text.strip()
    if _NARRATION.match(stripped):
        return True
    return _POSITIVE.search(stripped) is not None


def _usable_target(token: str) -> bool:
    if len(token) < 3 or token.lower() in _GENERIC_TARGETS:
        return False
    if any(mark in token for mark in (".", ";", "/", "\\", "(", ")")):
        return True
    if any(character.isupper() for character in token):
        return True
    return False


def _looks_like_path(rel: str) -> bool:
    if not rel or any(character.isspace() for character in rel):
        return False
    return "/" in rel or Path(rel).suffix.lower() in _CODE_SUFFIXES | {".md"}


def _within(project: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(project.resolve())
    except (OSError, ValueError):
        return False
    return True


def _find_named(project: Path, name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    seen = 0
    for path in project.rglob(name):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        seen += 1
        if seen > 20:
            return False
        if path.is_file() and _within(project, path):
            return True
    return False


def _content_has(project: Path, needle: str) -> bool:
    if len(needle) < 3 or not project.is_dir():
        return False
    seen = 0
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES and path.suffix.lower() != "":
            # Extensionless files are rare; skip markdown and other docs.
            if path.suffix:
                continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 1_000_000:
            continue
        seen += 1
        if seen > _CONTENT_FILE_CAP:
            return False
        try:
            data = path.read_bytes()[:_CONTENT_BYTES]
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        if needle in data.decode("utf-8", errors="replace"):
            return True
    return False


def _file_excerpts(project: Path, names: list[str]) -> str:
    if not names:
        return "(no changed files)"
    blocks: list[str] = []
    used = 0
    omitted: list[str] = []
    for name in names:
        if used >= EXCERPT_TOTAL:
            omitted.append(name)
            continue
        budget = min(EXCERPT_PER_FILE, EXCERPT_TOTAL - used)
        excerpt = _read_excerpt(project, name, budget)
        blocks.append(f"### {name}\n{excerpt}".rstrip())
        used += len(excerpt)
    if omitted:
        listed = "\n".join(f"- {name}" for name in omitted)
        blocks.append(f"Further changed files omitted from the excerpt:\n{listed}")
    return "\n\n".join(blocks)


def _read_excerpt(project: Path, rel: str, limit: int) -> str:
    path = project / rel
    if not path.exists():
        return "(not in the tree)\n"
    if path.is_dir():
        return "(directory)\n"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"(could not read: {exc})\n"
    if size > 1_000_000:
        return f"(file is {size} bytes; contents omitted)\n"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"(could not read: {exc})\n"
    if b"\0" in data[:8192]:
        return f"(binary file, {size} bytes)\n"
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(text) <= limit:
        return text if text.endswith("\n") else text + "\n"
    return text[:limit] + f"\n… [excerpt truncated, {len(text)} characters total]\n"
