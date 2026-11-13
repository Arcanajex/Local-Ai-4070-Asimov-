"""Task template checks and review-verdict parsing.

Strict mode is how a planning bot's markdown is caught before the local model
spends an overnight run on a vague spec. `--no-strict` skips these checks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REQUIRED_SECTIONS = ("Goal", "Acceptance criteria", "Out of scope")
_PY_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "pytest.ini",
    "Pipfile",
    "tox.ini",
)
_OTHER_MARKERS = (
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "Gemfile",
)


# Plain lines the model uses as headings, in addition to markdown # headings.
_PLAIN_HEADINGS = {
    "goal",
    "relevant files",
    "conventions and decisions",
    "acceptance criteria",
    "out of scope",
    "out-of-scope files touched",
    "conflicts with agents.md or decisions",
    "open questions",
    "open questions policy",
    "correctness",
    "tests",
    "risks",
    "verdict",
}


def _heading(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
    if match:
        return len(match.group(1)), match.group(2).strip().rstrip(":").lower()
    plain = stripped.rstrip(":").strip().lower()
    if plain in _PLAIN_HEADINGS:
        return 2, plain
    return None


def heading_exists(body: str, title: str) -> bool:
    wanted = title.strip().rstrip(":").lower()
    for line in body.splitlines():
        found = _heading(line)
        if found and found[1] == wanted:
            return True
    return False


def extract_section(body: str, title: str) -> str:
    """Return the body under a heading, not including the heading line."""

    lines = body.splitlines()
    start = None
    level = 0
    wanted = title.strip().rstrip(":").lower()
    for index, line in enumerate(lines):
        found = _heading(line)
        if found and found[1] == wanted:
            start = index + 1
            level = found[0]
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        found = _heading(lines[index])
        if found and found[0] <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def project_is_python(project: Path) -> bool:
    """Python projects may omit test_command; they default to pytest.

    An empty or unrecognized tree is treated as Python so a brand-new repo
    still gets the historical default. A Go, Node, Rust, or .NET tree does not.
    """

    if not project.is_dir():
        return True
    if any((project / name).is_file() for name in _PY_MARKERS):
        return True
    if any(project.glob("*.py")) or any(project.glob("*/*.py")):
        return True
    if any((project / name).exists() for name in _OTHER_MARKERS):
        return False
    if any(project.glob("*.csproj")) or any(project.glob("*.sln")):
        return False
    return True


def validate_task(task: Any, project: Path) -> list[str]:
    label = task.path.name if task.path else task.title or "task"
    problems: list[str] = []
    for section in REQUIRED_SECTIONS:
        if not heading_exists(task.body, section):
            problems.append(f"{label}: missing section: {section}")
    if not task.test_command_explicit and not project_is_python(project):
        problems.append(
            f"{label}: test_command is required because this project is not Python "
            "(Python projects default to pytest)"
        )
    return problems


def validate_task_files(paths: list[Path], project: Path) -> list[str]:
    from local_agent.overnight import load_task

    problems: list[str] = []
    for path in paths:
        try:
            task = load_task(path)
        except OSError as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        problems.extend(validate_task(task, project))
    return problems


def format_problems(problems: list[str]) -> str:
    lines = [
        "Refusing to start. These tasks do not match the template (docs/TASK_TEMPLATE.md):",
        *(f"- {item}" for item in problems),
        "Pass --no-strict (scripts: -NoStrict) to run old tasks anyway.",
    ]
    return "\n".join(lines)


def collect_task_paths(target: Path) -> list[Path]:
    if target.is_dir():
        from local_agent.queue import list_task_files

        return list_task_files(target)
    if target.is_file():
        return [target]
    raise FileNotFoundError(f"task path does not exist: {target}")


def review_problems(text: str) -> list[str]:
    """Reasons the self-review must trigger a fix instead of a merge.

    A criterion marked 'not met', a real conflict, or a missing required
    section fails the review. 'met' alone does not.
    """

    problems: list[str] = []
    if not heading_exists(text, "Acceptance criteria"):
        problems.append("review omitted the Acceptance criteria section")
    else:
        for line in extract_section(text, "Acceptance criteria").splitlines():
            if re.search(r"\bnot met\b", line, re.I):
                problems.append(line.strip())
    if not heading_exists(text, "Out-of-scope files touched"):
        problems.append("review omitted the Out-of-scope files touched section")
    if not heading_exists(text, "Conflicts with AGENTS.md or decisions"):
        problems.append("review omitted the Conflicts with AGENTS.md or decisions section")
    else:
        conflicts = extract_section(text, "Conflicts with AGENTS.md or decisions")
        if conflicts.strip() and not _is_none(conflicts):
            first = conflicts.strip().splitlines()[0]
            problems.append(f"conflict: {first[:200]}")
    if re.search(r"(?im)^verdict:\s*fix\b", text):
        problems.append("verdict: fix")
    return problems


def _is_none(text: str) -> bool:
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line.strip()
            break
    cleaned = first.lower().strip("-").strip().rstrip(".").rstrip(":")
    return cleaned in {
        "none",
        "no",
        "n/a",
        "na",
        "no conflict",
        "no conflicts",
        "no conflicts found",
        "nothing",
        "none found",
    }
