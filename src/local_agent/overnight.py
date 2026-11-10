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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from local_agent.client import ChatClient
from local_agent.config import Profile, get_profile
from local_agent.context import shrink_text
from local_agent.harness import AgentHarness, PhaseResult
from local_agent.logging_util import RunLogger
from local_agent.prompts import fix_prompt, implement_prompt, plan_prompt, review_prompt
from local_agent.tools import Workspace, clear_bytecode, run_command

PLAN_FILE = "AGENT_PLAN.md"
REPORT_FILE = "AGENT_REPORT.md"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class TaskSpec:
    body: str
    title: str
    test_command: str
    lint_command: str | None
    path: Path | None = None


@dataclass
class OvernightResult:
    status: str
    branch: str
    report_path: Path | None
    commit: str | None
    iterations: int
    tests_passed: bool
    log_dir: Path


def parse_task(text: str, path: Path | None = None) -> TaskSpec:
    meta, body = _split_front_matter(text)
    title = _title(body) or (path.stem if path else "task")
    test_command = str(meta.get("test_command") or "python -m pytest -q")
    lint_raw = meta.get("lint_command")
    lint_command = str(lint_raw) if lint_raw else None
    return TaskSpec(
        body=body.strip(),
        title=title,
        test_command=test_command,
        lint_command=lint_command,
        path=path,
    )


def load_task(path: Path) -> TaskSpec:
    return parse_task(Path(path).read_text(encoding="utf-8"), Path(path))


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[match.end() :]


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
) -> OvernightResult:
    project = Path(project).resolve()
    logger = RunLogger(log_dir)
    started = time.monotonic()
    deadline = started + max_seconds
    test_cmd = test_command or task.test_command
    lint_cmd = lint_command if lint_command is not None else task.lint_command
    workspace = Workspace(project)
    branch = ""
    iterations = 0
    tests_passed = False
    test_output = ""
    plan_status = "not_started"
    try:
        _require_clean_git(project)
        branch = _create_branch(project, slugify(task.title))
        logger.event("run_start", branch=branch, profile=profile.name, task=task.title, summary=branch)

        plan = _run_phase(
            client, workspace, logger, profile,
            plan_prompt(task.body),
            allowed={"read"},
            max_turns=max_plan_turns,
            deadline=deadline,
            phase="plan",
        )
        plan_status = plan.status
        plan_text = plan.content.strip() or "(the model did not return a plan)"
        (project / PLAN_FILE).write_text(_plan_document(task, plan_text), encoding="utf-8", newline="\n")

        if time.monotonic() >= deadline:
            return _finish(
                project, logger, task, profile, branch, log_dir,
                status="time_budget", iterations=0, tests_passed=False,
                test_output="", review="Stopped before implementation because the time budget was spent.",
                plan_status=plan.status,
            )

        implement = _run_phase(
            client, workspace, logger, profile,
            implement_prompt(task.body, plan_text),
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
                fix_prompt(task.body, shrink_text(test_output, 12_000), _status_short(project)),
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
        if time.monotonic() < deadline:
            diff = shrink_text(_diff(project), 24_000)
            review = _run_phase(
                client, workspace, logger, profile,
                review_prompt(task.body, diff, tests_passed),
                allowed=set(),
                max_turns=2,
                deadline=deadline,
                phase="review",
            )
            review_text = review.content.strip()
        report_status = "passed" if tests_passed else stop_status
        return _finish(
            project, logger, task, profile, branch, log_dir,
            status=report_status,
            iterations=iterations,
            tests_passed=tests_passed,
            test_output=test_output,
            review=review_text,
            plan_status=plan.status,
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
                )
            except Exception:
                pass
        raise
    finally:
        logger.close()


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
    )
    report_path = project / REPORT_FILE
    report_path.write_text(report, encoding="utf-8", newline="\n")
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
) -> str:
    return (
        f"# Agent report\n\n"
        f"- Status: {status}\n"
        f"- Tests passed: {'yes' if tests_passed else 'no'}\n"
        f"- Profile: {profile_name}\n"
        f"- Task: {task.title}\n"
        f"- Branch: {branch}\n"
        f"- Test iterations: {iterations}\n"
        f"- Plan phase: {plan_status}\n"
        f"- Log: {log_dir}\n\n"
        f"## Review\n\n{review.strip() or '(no review)'}\n\n"
        f"## Last test output\n\n```\n{shrink_text(test_output or '(tests did not run)', 8000)}\n```\n"
    )


def _plan_document(task: TaskSpec, plan: str) -> str:
    return f"# Plan: {task.title}\n\n{plan.strip()}\n"


def _require_clean_git(project: Path) -> None:
    if not (project / ".git").exists():
        raise RuntimeError(f"{project} is not a git repository. Initialize it before an overnight run.")
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
    identity = _git_identity_args(project)
    _git(project, *identity, "commit", "-m", message)
    return _git(project, "rev-parse", "--short", "HEAD").strip()


def _git_identity_args(project: Path) -> list[str]:
    email = _git(project, "config", "user.email", check=False).strip()
    name = _git(project, "config", "user.name", check=False).strip()
    args: list[str] = []
    if not name:
        args.extend(["-c", "user.name=Local Coding Agent"])
    if not email:
        args.extend(["-c", "user.email=local-agent@localhost"])
    return args


def _diff(project: Path) -> str:
    return _git(project, "diff", "--stat") + "\n\n" + _git(project, "diff")


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
