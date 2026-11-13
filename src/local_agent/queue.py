"""Run a folder of task files one at a time.

Each file goes through the overnight runner. When tests pass, the agent
branch is merged with --no-ff back into the branch the project was on when
the queue started, and the next file starts from that merge. A failure stops
the queue so a later step cannot build on work that never landed. Pass
continue_on_failure to keep going from the last successful merge anyway.

Progress is stored in queue-state.json in the task folder. Re-running the
same command skips any task already recorded as passed. Passed files are
moved to done/, failed files to failed/. Move a file from failed/ back to
the task folder to retry it.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from local_agent.config import Profile
from local_agent.overnight import (
    OvernightResult,
    _commit_with_identity,
    _git,
    ensure_repository,
    load_task,
    run_overnight,
)

STATE_NAME = "queue-state.json"
SUMMARY_NAME = "queue-summary.md"
SKIP_NAMES = {SUMMARY_NAME.lower(), "readme.md"}
RunTask = Callable[..., OvernightResult]


@dataclass
class QueueResult:
    status: str
    base_branch: str
    passed: int
    failed: int
    skipped: int
    stopped_on: str | None
    summary_path: Path
    state_path: Path
    tasks: list[dict[str, Any]] = field(default_factory=list)


def natural_key(name: str) -> list[int | str]:
    """Sort 2-a.md before 10-b.md. Digit runs compare as integers."""

    parts = re.split(r"(\d+)", name)
    key: list[int | str] = []
    for part in parts:
        if not part:
            continue
        key.append(int(part) if part.isdigit() else part.lower())
    return key


def list_task_files(task_dir: Path) -> list[Path]:
    """Top-level task markdown, in natural filename order.

    done/, failed/, the summary, and a README in the folder are not tasks.
    """

    files = [
        path
        for path in task_dir.glob("*.md")
        if path.is_file() and path.name.lower() not in SKIP_NAMES
    ]
    return sorted(files, key=lambda path: natural_key(path.name))


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "tasks": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object")
    data.setdefault("version", 1)
    data.setdefault("tasks", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def pending_tasks(task_dir: Path, state: dict[str, Any]) -> tuple[list[Path], list[str]]:
    """Return (to_run, skipped_names).

    A task recorded as passed is skipped even if its file is still in the
    folder, which covers a crash between the commit and the move into done/.
    """

    passed = {
        name
        for name, record in (state.get("tasks") or {}).items()
        if isinstance(record, dict) and record.get("status") == "passed"
    }
    skipped = [name for name in passed]
    pending = [path for path in list_task_files(task_dir) if path.name not in passed]
    return pending, skipped


def _refuse_protected_branch(branch: str, *, allow_main: bool) -> None:
    if branch.lower() not in {"main", "master"} or allow_main:
        return
    raise RuntimeError(
        f"The queue would merge into {branch}. Use an integration branch so a batch "
        f"does not land on {branch} directly. Check out that branch, or pass --allow-main "
        f"(scripts/queue.ps1 -AllowMain) to merge into {branch} anyway."
    )


def current_branch(project: Path) -> str:
    name = _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not name or name == "HEAD":
        raise RuntimeError(
            f"{project} is in detached HEAD. Check out the branch the queue should merge into."
        )
    return name


def merge_no_ff(project: Path, base_branch: str, agent_branch: str) -> str:
    """Check out base_branch and merge agent_branch with --no-ff.

    Returns the merge commit. On conflict the merge is aborted and the
    exception is raised with the repository back on base_branch.
    """

    _git(project, "checkout", base_branch)
    try:
        _commit_with_identity(
            project,
            "merge",
            "--no-ff",
            agent_branch,
            "-m",
            f"queue: merge {agent_branch}",
        )
    except RuntimeError:
        _git(project, "merge", "--abort", check=False)
        _git(project, "checkout", base_branch, check=False)
        raise
    return _git(project, "rev-parse", "--short", "HEAD").strip()


def render_summary(
    *,
    status: str,
    project: Path,
    base_branch: str,
    profile_name: str,
    tasks: list[dict[str, Any]],
    stopped_on: str | None,
) -> str:
    lines = [
        "# Queue summary",
        "",
        f"- Status: {status}",
        f"- Project: {project}",
        f"- Base branch: {base_branch}",
        f"- Profile: {profile_name}",
    ]
    if stopped_on:
        lines.append(f"- Stopped on: {stopped_on}")
    lines.extend(
        [
            "",
            "| Task | Status | Branch | Commit | Iterations | Duration |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    ordered = sorted(tasks, key=lambda record: natural_key(str(record.get("name", ""))))
    if not ordered:
        lines.append("| (none) |  |  |  |  |  |")
    for record in ordered:
        lines.append(
            "| {name} | {status} | {branch} | {commit} | {iterations} | {duration} |".format(
                name=record.get("name", ""),
                status=record.get("status", ""),
                branch=record.get("branch") or "",
                commit=record.get("commit") or "",
                iterations=record.get("iterations", ""),
                duration=_format_duration(record.get("duration_seconds")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _format_duration(seconds: Any) -> str:
    if seconds is None or seconds == "":
        return ""
    value = float(seconds)
    if value < 90:
        return f"{value:.1f}s"
    minutes, secs = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def run_queue(
    *,
    task_dir: Path,
    project: Path,
    profile: Profile,
    client: Any,
    log_dir: Path,
    max_seconds_per_task: float = 8 * 3600,
    max_iterations: int = 6,
    init_repo: bool = False,
    continue_on_failure: bool = False,
    strict: bool = True,
    allow_main: bool = False,
    run_task: RunTask | None = None,
) -> QueueResult:
    task_dir = Path(task_dir).resolve()
    project = Path(project).resolve()
    log_dir = Path(log_dir)
    if not task_dir.is_dir():
        raise FileNotFoundError(f"task folder does not exist: {task_dir}")
    ensure_repository(project, init_repo=init_repo)

    state_path = task_dir / STATE_NAME
    summary_path = task_dir / SUMMARY_NAME
    state = load_state(state_path)
    starting_branch = current_branch(project)
    base_branch = str(state.get("base_branch") or "") or starting_branch
    _refuse_protected_branch(starting_branch, allow_main=allow_main)
    _refuse_protected_branch(base_branch, allow_main=allow_main)
    if allow_main and base_branch.lower() in {"main", "master"}:
        print(
            f"warning: this queue will merge into {base_branch}. "
            "An integration branch keeps a batch off the default branch.",
            file=sys.stderr,
        )
    state["base_branch"] = base_branch
    state["project"] = str(project)
    state["profile"] = profile.name

    pending, skipped_names = pending_tasks(task_dir, state)
    if strict:
        from local_agent.taskcheck import format_problems, validate_task_files

        problems = validate_task_files(list_task_files(task_dir), project)
        if problems:
            raise RuntimeError(format_problems(problems))
    if not pending and not state["tasks"]:
        raise RuntimeError(
            f"No task markdown files in {task_dir}. "
            "Add files such as 01-runner.md. README.md and queue-summary.md are ignored."
        )

    runner = run_task or _default_run_task
    stopped_on: str | None = None
    stopped_early = False
    log_dir.mkdir(parents=True, exist_ok=True)

    def persist(*, in_progress: bool = False) -> None:
        save_state(state_path, state)
        if in_progress and not stopped_early:
            status = "running"
        else:
            status = _overall_status(state, stopped_early)
        summary = render_summary(
            status=status,
            project=project,
            base_branch=base_branch,
            profile_name=profile.name,
            tasks=list(state["tasks"].values()),
            stopped_on=stopped_on,
        )
        summary_path.write_text(summary, encoding="utf-8", newline="\n")

    for path in pending:
        _git(project, "checkout", base_branch)
        task = load_task(path)
        task_log = log_dir / path.stem
        started = time.perf_counter()
        try:
            result = runner(
                task=task,
                project=project,
                profile=profile,
                client=client,
                log_dir=task_log,
                max_seconds=max_seconds_per_task,
                max_iterations=max_iterations,
                init_repo=False,
                strict=strict,
            )
        except Exception as exc:
            duration = time.perf_counter() - started
            moved = _move_task(path, task_dir / "failed")
            _record(
                state,
                path.name,
                status="error",
                branch="",
                commit=None,
                merge_commit=None,
                iterations=0,
                duration=duration,
                detail=str(exc),
                stored_at=str(moved),
            )
            if not continue_on_failure:
                stopped_on = path.name
                stopped_early = True
            persist(in_progress=True)
            _return_to_base(project, base_branch)
            if not continue_on_failure:
                break
            continue

        duration = time.perf_counter() - started
        if result.tests_passed and result.review_passed:
            try:
                merge_commit = merge_no_ff(project, base_branch, result.branch) if result.branch else ""
            except Exception as exc:
                moved = _move_task(path, task_dir / "failed")
                _record(
                    state,
                    path.name,
                    status="merge_failed",
                    branch=result.branch,
                    commit=result.commit,
                    merge_commit=None,
                    iterations=result.iterations,
                    duration=duration,
                    detail=str(exc),
                    stored_at=str(moved),
                )
                stopped_on = path.name
                stopped_early = True
                persist(in_progress=True)
                _return_to_base(project, base_branch)
                break
            moved = _move_task(path, task_dir / "done")
            _record(
                state,
                path.name,
                status="passed",
                branch=result.branch,
                commit=result.commit,
                merge_commit=merge_commit,
                iterations=result.iterations,
                duration=duration,
                detail="",
                stored_at=str(moved),
            )
            persist(in_progress=True)
            continue

        moved = _move_task(path, task_dir / "failed")
        _record(
            state,
            path.name,
            status=result.status or "failed",
            branch=result.branch,
            commit=result.commit,
            merge_commit=None,
            iterations=result.iterations,
            duration=duration,
            detail="tests did not pass" if not result.tests_passed else "review rejected the change",
            stored_at=str(moved),
        )
        if not continue_on_failure:
            stopped_on = path.name
            stopped_early = True
        persist(in_progress=True)
        _return_to_base(project, base_branch)
        if not continue_on_failure:
            break

    # A passed file left in the task folder (move failed last time) is skipped.
    for name in skipped_names:
        source = task_dir / name
        record = state["tasks"].get(name) or {}
        if source.is_file() and not str(record.get("stored_at", "")).startswith(str(task_dir / "done")):
            moved = _move_task(source, task_dir / "done")
            record["stored_at"] = str(moved)
            state["tasks"][name] = record

    status = _overall_status(state, stopped_early)
    persist()
    records = list(state["tasks"].values())
    failed = sum(1 for record in records if record.get("status") != "passed")
    passed = sum(1 for record in records if record.get("status") == "passed")
    return QueueResult(
        status=status,
        base_branch=base_branch,
        passed=passed,
        failed=failed,
        skipped=len(skipped_names),
        stopped_on=stopped_on,
        summary_path=summary_path,
        state_path=state_path,
        tasks=sorted(records, key=lambda record: natural_key(str(record.get("name", "")))),
    )


def _default_run_task(**kwargs: Any) -> OvernightResult:
    return run_overnight(**kwargs)


def _overall_status(state: dict[str, Any], stopped_early: bool) -> str:
    records = list((state.get("tasks") or {}).values())
    if stopped_early:
        return "stopped"
    if any(record.get("status") != "passed" for record in records):
        return "completed_with_failures"
    return "passed"


def _record(
    state: dict[str, Any],
    name: str,
    *,
    status: str,
    branch: str,
    commit: str | None,
    merge_commit: str | None,
    iterations: int,
    duration: float,
    detail: str,
    stored_at: str,
) -> None:
    state["tasks"][name] = {
        "name": name,
        "status": status,
        "branch": branch,
        "commit": commit,
        "merge_commit": merge_commit,
        "iterations": iterations,
        "duration_seconds": round(duration, 3),
        "detail": detail,
        "stored_at": stored_at,
    }


def _move_task(source: Path, dest_dir: Path) -> Path:
    if not source.is_file():
        return source
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if dest.exists():
        dest = dest_dir / f"{source.stem}-{time.time_ns()}{source.suffix}"
    source.replace(dest)
    return dest


def _return_to_base(project: Path, base_branch: str) -> None:
    """Leave the project on the base branch so the next task does not fork a failure."""

    try:
        current = current_branch(project)
    except RuntimeError:
        current = ""
    if current == base_branch:
        return
    _git(project, "checkout", base_branch)
