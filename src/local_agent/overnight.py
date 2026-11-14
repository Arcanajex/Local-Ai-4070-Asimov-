"""Quality-focused overnight loop.

The task file goes in, then the model plans, implements, and the runner
itself executes the test command. Failures are handed back as a new, short
conversation. When tests pass or the iteration or time budget is spent, the
model reviews the diff with no tools. The runner commits the branch and
writes AGENT_REPORT.md. The model is not asked to commit: an unattended
`git commit` from the shell is how a run leaves the tree in a surprise state.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from local_agent.client import ChatClient
from local_agent.config import Profile, get_profile
from local_agent.context import shrink_text
from local_agent.harness import AgentHarness, PhaseResult
from local_agent.logging_util import RunLogger
from local_agent.project_context import (
    DEFAULT_CONTEXT_CHARS,
    build_project_context,
    shrink_context_to_budget,
    with_project_context,
)
from local_agent.prompts import (
    fix_prompt,
    implement_prompt,
    plan_prompt,
    review_fix_prompt,
    review_prompt,
)
from local_agent.taskcheck import extract_section, format_problems, review_problems, validate_task
from local_agent.tools import Workspace, clear_bytecode, run_command

PLAN_FILE = "AGENT_PLAN.md"
REPORT_FILE = "AGENT_REPORT.md"
AGENT_DIR_NAME = ".agent"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class TaskSpec:
    body: str
    title: str
    test_command: str
    lint_command: str | None
    path: Path | None = None
    test_command_explicit: bool = False
    context_paths: list[str] = field(default_factory=list)


@dataclass
class OvernightResult:
    status: str
    branch: str
    report_path: Path | None
    commit: str | None
    iterations: int
    tests_passed: bool
    log_dir: Path
    review_passed: bool = True


def parse_task(text: str, path: Path | None = None) -> TaskSpec:
    meta, context_paths, body = _split_front_matter(text)
    title = _title(body) or (path.stem if path else "task")
    explicit = "test_command" in meta
    test_command = str(meta["test_command"]) if explicit else "python -m pytest -q"
    lint_raw = meta.get("lint_command")
    lint_command = str(lint_raw) if lint_raw else None
    return TaskSpec(
        body=body.strip(),
        title=title,
        test_command=test_command,
        lint_command=lint_command,
        path=path,
        test_command_explicit=explicit,
        context_paths=context_paths,
    )


def load_task(path: Path) -> TaskSpec:
    return parse_task(Path(path).read_text(encoding="utf-8"), Path(path))


def _split_front_matter(text: str) -> tuple[dict[str, str], list[str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, [], text
    meta, context_paths = _parse_front_matter(match.group(1))
    return meta, context_paths, text[match.end() :]


def _parse_front_matter(block: str) -> tuple[dict[str, str], list[str]]:
    """Line-oriented front matter, including a `context` list."""

    meta: dict[str, str] = {}
    context: list[str] = []
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "context" and not value:
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith("- "):
                item = lines[index].lstrip()[2:].strip().strip('"').strip("'")
                if item:
                    context.append(item)
                index += 1
            continue
        if key == "context":
            context.extend(part.strip() for part in value.split(",") if part.strip())
        else:
            meta[key] = value
        index += 1
    return meta, context


def _title(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "task"


def run_overnight(
    *,
    task: TaskSpec,
    project: Path,
    profile: Profile,
    client: ChatClient,
    log_dir: Path,
    max_iterations: int = 6,
    max_seconds: float = 8 * 3600,
    max_plan_turns: int = 8,
    max_work_turns: int = 30,
    test_command: str | None = None,
    lint_command: str | None = None,
    init_repo: bool = False,
    strict: bool = True,
    context_char_limit: int = DEFAULT_CONTEXT_CHARS,
) -> OvernightResult:
    project = Path(project).resolve()
    logger = RunLogger(log_dir)
    started = time.monotonic()
    deadline = started + max_seconds
    test_cmd = test_command or task.test_command
    lint_cmd = lint_command if lint_command is not None else task.lint_command
    branch = ""
    iterations = 0
    tests_passed = False
    test_output = ""
    plan_status = "not_started"
    identity_note = ""
    review_passed = True
    context_block = ""
    try:
        identity_note = ensure_repository(project, init_repo=init_repo) or ""
        if identity_note:
            logger.event("git_identity", summary=identity_note)
            print(identity_note, file=sys.stderr)
        _require_clean_git(project)
        exclude_agent_dir(project)
        if strict:
            problems = validate_task(task, project)
            if problems:
                raise RuntimeError(format_problems(problems))
        workspace = Workspace(project)
        context_block = shrink_context_to_budget(
            build_project_context(project, task.context_paths, limit=context_char_limit),
            context_tokens=profile.context,
            generation_tokens=profile.agent_max_tokens_ceiling,
        )
        logger.event(
            "project_context",
            chars=len(context_block),
            paths=["AGENTS.md", *task.context_paths],
            summary=f"project context {len(context_block)} chars",
        )
        branch = _create_branch(project, slugify(task.title))
        logger.event("run_start", branch=branch, profile=profile.name, task=task.title, summary=branch)

        plan = _run_phase(
            client, workspace, logger, profile,
            with_project_context(plan_prompt(task.body), context_block),
            allowed={"read"},
            max_turns=max_plan_turns,
            deadline=deadline,
            phase="plan",
        )
        plan_status = plan.status
        plan_text = plan.content.strip() or "(the model did not return a plan)"
        _write_agent_file(project, PLAN_FILE, _plan_document(task, plan_text))

        if time.monotonic() >= deadline:
            return _finish(
                project, logger, task, profile, branch, log_dir,
                status="time_budget", iterations=0, tests_passed=False,
                test_output="", review="Stopped before implementation because the time budget was spent.",
                plan_status=plan.status,
                identity_note=identity_note,
            )

        implement = _run_phase(
            client, workspace, logger, profile,
            with_project_context(implement_prompt(task.body, plan_text), context_block),
            allowed=None,
            max_turns=max_work_turns,
            deadline=deadline,
            phase="implement",
        )
        tests_passed = False
        test_output = ""
        iterations = 0
        stop_status = implement.status
        for iterations in range(1, max_iterations + 1):
            if time.monotonic() >= deadline:
                stop_status = "time_budget"
                break
            test_output, tests_passed = _run_checks(project, test_cmd, lint_cmd)
            logger.event(
                "tests",
                iteration=iterations,
                passed=tests_passed,
                output=test_output,
                summary=f"iteration {iterations} passed={tests_passed}",
            )
            if tests_passed:
                stop_status = "passed"
                break
            if iterations == max_iterations:
                stop_status = "iteration_budget"
                break
            if time.monotonic() >= deadline:
                stop_status = "time_budget"
                break
            fix = _run_phase(
                client, workspace, logger, profile,
                with_project_context(
                    fix_prompt(task.body, shrink_text(test_output, 12_000), _status_short(project)),
                    context_block,
                ),
                allowed=None,
                max_turns=max_work_turns,
                deadline=deadline,
                phase=f"fix-{iterations}",
            )
            if fix.status in {"time_budget", "empty_reply", "loop_detected"}:
                stop_status = fix.status
                # Still run the checks once more if the model managed to edit before stopping.
                if fix.status != "time_budget":
                    test_output, tests_passed = _run_checks(project, test_cmd, lint_cmd)
                    logger.event(
                        "tests",
                        iteration=iterations,
                        passed=tests_passed,
                        output=test_output,
                        summary=f"after {fix.status} passed={tests_passed}",
                    )
                    if tests_passed:
                        stop_status = "passed"
                break
        else:
            stop_status = "iteration_budget"

        review_text = ""
        review_passed = False
        if time.monotonic() < deadline:
            review_text, review_passed, tests_passed, test_output, stop_status = _review_and_maybe_fix(
                client=client,
                workspace=workspace,
                logger=logger,
                profile=profile,
                task=task,
                context_block=context_block,
                tests_passed=tests_passed,
                test_output=test_output,
                stop_status=stop_status,
                deadline=deadline,
                max_work_turns=max_work_turns,
                test_command=test_cmd,
                lint_command=lint_cmd,
            )
        if tests_passed and review_passed:
            report_status = "passed"
        elif not tests_passed:
            report_status = stop_status
        else:
            report_status = "review_failed"
        return _finish(
            project, logger, task, profile, branch, log_dir,
            status=report_status,
            iterations=iterations,
            tests_passed=tests_passed,
            test_output=test_output,
            review=review_text,
            plan_status=plan.status,
            identity_note=identity_note,
            review_passed=review_passed,
            open_questions=_open_questions(plan_text, review_text),
        )
    except Exception as exc:
        logger.event("run_error", summary=f"{type(exc).__name__}: {exc}")
        if branch:
            try:
                return _finish(
                    project, logger, task, profile, branch, log_dir,
                    status="error",
                    iterations=iterations,
                    tests_passed=tests_passed,
                    test_output=test_output,
                    review=f"The run stopped with an error:\n\n{exc}",
                    plan_status=plan_status,
                    identity_note=identity_note,
                )
            except Exception:
                pass
        raise
    finally:
        logger.close()


def _review_and_maybe_fix(
    *,
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    task: TaskSpec,
    context_block: str,
    tests_passed: bool,
    test_output: str,
    stop_status: str,
    deadline: float,
    max_work_turns: int,
    test_command: str,
    lint_command: str | None,
) -> tuple[str, bool, bool, str, str]:
    """Review once. A not-met criterion or a conflict gets one fix, then a re-review."""

    review_text, problems = _one_review(
        client, workspace, logger, profile, task, context_block, tests_passed, deadline, phase="review"
    )
    if not problems:
        return review_text, True, tests_passed, test_output, stop_status
    logger.event("review_rejected", summary="; ".join(problems)[:500], problems=problems)
    if time.monotonic() >= deadline:
        return review_text, False, tests_passed, test_output, stop_status
    _run_phase(
        client, workspace, logger, profile,
        with_project_context(
            review_fix_prompt(task.body, shrink_text(review_text, 12_000), _status_short(workspace.root)),
            context_block,
        ),
        allowed=None,
        max_turns=max_work_turns,
        deadline=deadline,
        phase="review-fix",
    )
    test_output, tests_passed = _run_checks(workspace.root, test_command, lint_command)
    logger.event(
        "tests",
        iteration="review-fix",
        passed=tests_passed,
        output=test_output,
        summary=f"after review fix passed={tests_passed}",
    )
    if not tests_passed:
        return review_text, False, tests_passed, test_output, "tests_failed"
    if time.monotonic() >= deadline:
        return review_text, False, tests_passed, test_output, stop_status
    review_text, problems = _one_review(
        client, workspace, logger, profile, task, context_block, tests_passed, deadline, phase="review-again"
    )
    return review_text, not problems, tests_passed, test_output, stop_status


def _one_review(
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    task: TaskSpec,
    context_block: str,
    tests_passed: bool,
    deadline: float,
    *,
    phase: str,
) -> tuple[str, list[str]]:
    diff = fit_review_diff(working_tree_diff(workspace.root), 24_000)
    review = _run_phase(
        client, workspace, logger, profile,
        review_prompt(
            task.body,
            diff,
            tests_passed,
            acceptance=extract_section(task.body, "Acceptance criteria"),
            out_of_scope=extract_section(task.body, "Out of scope"),
            context_block=context_block,
        ),
        allowed=set(),
        max_turns=2,
        deadline=deadline,
        phase=phase,
    )
    text = review.content.strip()
    return text, review_problems(text)


def _run_phase(
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    prompt: str,
    *,
    allowed: set[str] | None,
    max_turns: int,
    deadline: float,
    phase: str,
) -> PhaseResult:
    logger.event("phase_start", phase=phase, summary=phase)
    harness = AgentHarness(
        client, workspace, logger, profile, allowed_tools=allowed
    )
    result = harness.run(prompt, max_turns=max_turns, deadline=deadline)
    logger.event("phase_end", phase=phase, status=result.status, summary=f"{phase} {result.status}")
    return result


def _run_checks(project: Path, test_command: str, lint_command: str | None) -> tuple[str, bool]:
    clear_bytecode(project)
    test = run_command(test_command, cwd=project, timeout=600)
    parts = [f"$ {test_command}\n{test.output}"]
    ok = test.ok
    if ok and lint_command:
        lint = run_command(lint_command, cwd=project, timeout=600)
        parts.append(f"$ {lint_command}\n{lint.output}")
        ok = lint.ok
    return "\n\n".join(parts), ok


def _finish(
    project: Path,
    logger: RunLogger,
    task: TaskSpec,
    profile: Profile,
    branch: str,
    log_dir: Path,
    *,
    status: str,
    iterations: int,
    tests_passed: bool,
    test_output: str,
    review: str,
    plan_status: str,
    identity_note: str = "",
    review_passed: bool = True,
    open_questions: str = "",
) -> OvernightResult:
    report = _render_report(
        task=task,
        profile_name=profile.name,
        branch=branch,
        status=status,
        iterations=iterations,
        tests_passed=tests_passed,
        test_output=test_output,
        review=review,
        plan_status=plan_status,
        log_dir=log_dir,
        identity_note=identity_note,
        open_questions=open_questions,
        review_passed=review_passed,
    )
    exclude_agent_dir(project)
    report_path = _write_agent_file(project, REPORT_FILE, report)
    commit = _commit(project, task.title, status)
    logger.event("run_end", status=status, commit=commit, summary=f"{status} commit={commit}")
    return OvernightResult(
        status=status,
        branch=branch,
        report_path=report_path,
        commit=commit,
        iterations=iterations,
        tests_passed=tests_passed,
        log_dir=log_dir,
        review_passed=review_passed,
    )


def _render_report(
    *,
    task: TaskSpec,
    profile_name: str,
    branch: str,
    status: str,
    iterations: int,
    tests_passed: bool,
    test_output: str,
    review: str,
    plan_status: str,
    log_dir: Path,
    identity_note: str = "",
    open_questions: str = "",
    review_passed: bool = True,
) -> str:
    note = f"\n{identity_note}\n" if identity_note else ""
    questions = open_questions.strip() or "(none recorded)"
    return (
        f"# Agent report\n\n"
        f"- Status: {status}\n"
        f"- Tests passed: {'yes' if tests_passed else 'no'}\n"
        f"- Review passed: {'yes' if review_passed else 'no'}\n"
        f"- Profile: {profile_name}\n"
        f"- Task: {task.title}\n"
        f"- Branch: {branch}\n"
        f"- Test iterations: {iterations}\n"
        f"- Plan phase: {plan_status}\n"
        f"- Log: {log_dir}\n"
        f"{note}\n"
        f"## Review\n\n{review.strip() or '(no review)'}\n\n"
        f"## Open questions\n\n{questions}\n\n"
        f"## Last test output\n\n```\n{shrink_text(test_output or '(tests did not run)', 8000)}\n```\n"
    )


def _plan_document(task: TaskSpec, plan: str) -> str:
    return f"# Plan: {task.title}\n\n{plan.strip()}\n"


def agent_dir(project: Path) -> Path:
    return project / AGENT_DIR_NAME


def exclude_agent_dir(project: Path) -> None:
    """Keep `.agent/` out of commits via the repo-local exclude file.

    `.git/info/exclude` is not a file in the user's tree, so this does not
    edit `.gitignore` or any other committed file.
    """

    exclude = project / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = [line.strip() for line in existing.splitlines()]
    if ".agent/" in lines or ".agent" in lines:
        return
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    exclude.write_text(existing + suffix + ".agent/\n", encoding="utf-8")


def _write_agent_file(project: Path, name: str, text: str) -> Path:
    exclude_agent_dir(project)
    directory = agent_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _open_questions(*texts: str) -> str:
    chunks = []
    for text in texts:
        section = extract_section(text, "Open questions")
        if section.strip():
            chunks.append(section.strip())
    return "\n\n".join(chunks)


def fit_review_diff(diff_text: str, limit: int) -> str:
    """Keep the file list, then as much patch as fits, and name what was cut.

    A silent character trim used to drop the tail of the diff with no mention
    of the files the model never saw.
    """

    if len(diff_text) <= limit:
        return diff_text
    marker = "\n# Patch\n"
    if marker in diff_text:
        stat, patch = diff_text.split(marker, 1)
        stat = stat + marker
    else:
        stat, patch = "", diff_text
    note_room = 400
    budget = max(0, limit - len(stat) - note_room)
    included, omitted = _take_diff_files(patch, budget)
    note = ""
    if omitted:
        listed = "\n".join(f"- {name}" for name in omitted)
        note = (
            "\n\nFiles omitted from the patch (they are still in the file list above; "
            f"read them in the repo):\n{listed}\n"
        )
    # Keep the omission note even when the patch budget was optimistic.
    # Never cut the file list to make the note fit. A slightly long prompt is
    # better than a review that cannot see which files changed.
    return stat + included + note


def _take_diff_files(patch: str, budget: int) -> tuple[str, list[str]]:
    if not patch.strip():
        return "", []
    parts = re.split(r"(?=^diff --git )", patch, flags=re.M)
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for part in parts:
        if not part.strip():
            continue
        name = _diff_file_name(part) or part.splitlines()[0][:120]
        if used + len(part) <= budget:
            included.append(part)
            used += len(part)
        else:
            omitted.append(name)
    return "".join(included), omitted


def _diff_file_name(chunk: str) -> str:
    match = re.search(r"^diff --git a/.+ b/(.+)$", chunk, flags=re.M)
    if match:
        return match.group(1).strip()
    plus = re.search(r"^\+\+\+ b/(.+)$", chunk, flags=re.M)
    return plus.group(1).strip() if plus else ""


def ensure_repository(project: Path, *, init_repo: bool) -> str | None:
    """Make sure `project` is a git repo.

    With `init_repo`, a folder that is not a repository gets `git init` and an
    empty initial commit. Returns a note when git has no user.name or
    user.email, so the caller can show it. The note is None when identity is
    already configured. Global git config is never written.
    """

    if not (project / ".git").exists():
        if not init_repo:
            raise RuntimeError(
                f"{project} is not a git repository. "
                "Pass --init-repo to `python -m local_agent overnight`, "
                "or -InitRepo to scripts/overnight.ps1, "
                "to create the repository and an empty initial commit."
            )
        project.mkdir(parents=True, exist_ok=True)
        _git(project, "init", "-b", "main")
        _commit_with_identity(project, "commit", "--allow-empty", "-m", "Initial commit")
    return missing_identity_message(project)


def missing_identity_message(project: Path) -> str | None:
    missing = [key for key in ("user.name", "user.email") if not _config_get(project, key)]
    if not missing:
        return None
    listed = " and ".join(missing)
    return (
        f"Git {listed} is not set. "
        "This run will commit as Local Coding Agent <local-agent@localhost> "
        "without changing your git config. "
        'To use your own name: git config --global user.name "Your Name" '
        'and git config --global user.email "you@example.com".'
    )


def _require_clean_git(project: Path) -> None:
    if not (project / ".git").exists():
        raise RuntimeError(
            f"{project} is not a git repository. "
            "Pass --init-repo to `python -m local_agent overnight`, "
            "or -InitRepo to scripts/overnight.ps1, "
            "to create the repository and an empty initial commit."
        )
    status = _git(project, "status", "--porcelain")
    if status.strip():
        raise RuntimeError(
            f"{project} has uncommitted changes. Commit or stash them before an overnight run.\n{status}"
        )


def _create_branch(project: Path, slug: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = f"agent/{slug}-{stamp}"
    _git(project, "checkout", "-b", name)
    return name


def _commit(project: Path, title: str, status: str) -> str | None:
    _git(project, "add", "-A")
    staged = _git(project, "diff", "--cached", "--name-only")
    if not staged.strip():
        return None
    message = f"agent: {title} ({status})"
    _commit_with_identity(project, "commit", "-m", message)
    return _git(project, "rev-parse", "--short", "HEAD").strip()


def _commit_with_identity(project: Path, *args: str) -> str:
    """Commit even when user.name or user.email is unset.

    The fallback is a one-shot `git -c` and does not write git config.
    A rejection still says how to set the identity, instead of only pasting
    git's "please tell me who you are" text.
    """

    try:
        return _git(project, *_git_identity_args(project), *args)
    except RuntimeError as exc:
        text = str(exc)
        lowered = text.lower()
        if "user.email" in lowered or "user.name" in lowered or "identity unknown" in lowered:
            raise RuntimeError(
                "Git could not commit because user.name or user.email is missing, "
                "and the fallback identity was not accepted.\n"
                'Set them with: git config --global user.name "Your Name"\n'
                'and git config --global user.email "you@example.com".\n'
                f"{text}"
            ) from exc
        raise


def _git_identity_args(project: Path) -> list[str]:
    args: list[str] = []
    if not _config_get(project, "user.name"):
        args.extend(["-c", "user.name=Local Coding Agent"])
    if not _config_get(project, "user.email"):
        args.extend(["-c", "user.email=local-agent@localhost"])
    return args


def _config_get(project: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "config", "--get", key],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# Cap one new file so a generated log cannot push every other file out of the
# review prompt. The whole diff is capped again before it is sent.
_NEW_FILE_CHARS = 12_000


def working_tree_diff(project: Path) -> str:
    """Full change set against HEAD, including files git has never tracked.

    `git diff` and `git diff HEAD` only describe tracked files. A run that
    starts from an empty commit and creates textstats.py leaves those files
    untracked until the final `git add`, so a review that only ran `git diff`
    told the model the diff was missing.
    """

    tracked_stat = ""
    tracked_patch = ""
    if _head_exists(project):
        tracked_stat = _git(project, "diff", "HEAD", "--stat").strip()
        tracked_patch = _git(project, "diff", "HEAD").strip()
    untracked = [
        line.strip()
        for line in _git(project, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip()
    ]
    if not tracked_stat and not tracked_patch and not untracked:
        return "(no changes relative to HEAD)\n"
    parts = ["# Changes since HEAD", ""]
    if tracked_stat:
        parts.append(tracked_stat)
    if untracked:
        parts.append("Untracked files (new):")
        parts.extend(f"  {name}" for name in untracked)
    parts.extend(["", "# Patch", ""])
    if tracked_patch:
        parts.append(tracked_patch)
        parts.append("")
    for rel in untracked:
        parts.append(_new_file_patch(project, rel).rstrip())
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def _head_exists(project: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _new_file_patch(project: Path, rel: str) -> str:
    path = project / rel
    header = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n"
    if not path.is_file():
        return header + "(not a regular file)\n"
    size = path.stat().st_size
    if size > 1_000_000:
        return header + f"(new file, {size} bytes, contents omitted)\n"
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return header + f"Binary file {rel} added ({size} bytes)\n"
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    truncated = ""
    if len(text) > _NEW_FILE_CHARS:
        text = text[:_NEW_FILE_CHARS]
        truncated = "\\ [truncated]\n"
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    if not lines:
        return header + "@@ -0,0 +0,0 @@\n(empty file)\n"
    body = "".join(f"+{line}\n" for line in lines)
    return header + f"@@ -0,0 +1,{len(lines)} @@\n" + body + truncated


def _status_short(project: Path) -> str:
    return _git(project, "status", "--porcelain") or "(clean)"


def _git(project: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout


def make_client(profile: Profile, base_url: str | None = None) -> ChatClient:
    url = base_url or f"http://{profile.host}:{profile.port}"
    return ChatClient(url, profile)


def load_profile(name: str) -> Profile:
    return get_profile(name)
