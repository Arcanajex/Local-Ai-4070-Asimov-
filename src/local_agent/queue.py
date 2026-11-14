"""Run a folder of task files one at a time.

Each file goes through the overnight runner. When tests pass, the agent
branch is merged with --no-ff back into the branch the project was on when
the queue started, and the next file starts from that merge. A failure stops
the queue so a later step cannot build on work that never landed. Pass
continue_on_failure to keep going from the last successful merge anyway.

A passing file is moved into the archive (``tasks/archive`` in this repo by
default), keeping its path relative to the tasks root. A failing file stays
where it is so it can be fixed and run again. ``--no-archive`` keeps the older
layout: passed files move to ``done/`` and failed files to ``failed/``.

Markdown inside the queued folder is included, except ``README.md``,
``queue-summary.md``, and anything under ``done/``, ``failed/``, ``archive/``,
or the configured archive directory. That exclusion still applies when the
archive sits inside the queued folder.

Progress is stored in queue-state.json in the task folder. Re-running the
same command skips any task already recorded as passed.

Between tasks the queue looks for a stop file (``runtime/STOP_QUEUE``). When
the file is present it starts nothing else, writes a summary, deletes the
file, and returns. A stop file that already exists at startup is stale: the
queue warns, deletes it, and runs. The file is never checked while a task is
running.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from local_agent import archive as task_archive
from local_agent.config import Profile
from local_agent.control import clear_stop_file, default_stop_file, stop_is_requested
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
EXCLUDED_DIR_NAMES = {"done", "failed", "archive"}
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
    pending: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    stayed: list[str] = field(default_factory=list)
    message: str = ""


def natural_key(name: str) -> list[tuple[int, int | str]]:
    """Sort 2-a.md before 10-b.md. Digit runs compare as integers.

    Each piece is tagged so a number and a letter can be ordered in one list
    (``1-c.md`` before ``ui/step.md``) without mixing int and str compares.
    """

    parts = re.split(r"(\d+)", name)
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def task_key(path: Path, task_dir: Path) -> str:
    """Stable id for queue state. Top-level files stay ``name.md``."""

    return path.resolve().relative_to(Path(task_dir).resolve()).as_posix()


def list_task_files(task_dir: Path, *, archive_dir: Path | None = None) -> list[Path]:
    """Task markdown under ``task_dir``, in natural path order.

    ``done/``, ``failed/``, ``archive/``, hidden directories, and ``archive_dir``
    are not tasks. A queue pointed at the archive itself returns nothing.
    ``README.md`` and ``queue-summary.md`` are not tasks at any depth.
    """

    root = Path(task_dir).resolve()
    if not root.is_dir():
        return []
    archive_resolved = Path(archive_dir).resolve() if archive_dir is not None else None
    if _scan_excluded(root, archive_resolved):
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if not _scan_excluded(current / name, archive_resolved)
        ]
        for name in filenames:
            if not name.lower().endswith(".md") or name.lower() in SKIP_NAMES:
                continue
            path = current / name
            if archive_resolved is not None and _is_under(path.resolve(), archive_resolved):
                continue
            found.append(path)
    return sorted(found, key=lambda path: natural_key(path.relative_to(root).as_posix()))


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


def pending_tasks(
    task_dir: Path,
    state: dict[str, Any],
    *,
    archive_dir: Path | None = None,
) -> tuple[list[Path], list[str]]:
    """Return (to_run, skipped_names).

    A task recorded as passed is skipped even if its file is still in the
    folder, which covers a crash between the commit and the move.
    """

    passed = {
        name
        for name, record in (state.get("tasks") or {}).items()
        if isinstance(record, dict) and record.get("status") == "passed"
    }
    skipped = [name for name in passed]
    pending = [
        path
        for path in list_task_files(task_dir, archive_dir=archive_dir)
        if task_key(path, task_dir) not in passed
    ]
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
    pending: list[str] | None = None,
    stop_request: bool = False,
    late_stop_note: str = "",
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
    if stop_request:
        lines.append(
            "- Stopped on request: the current task was allowed to finish. "
            "No new task was started."
        )
        names = ", ".join(pending or []) or "(none)"
        lines.append(f"- Still pending: {names}")
    if late_stop_note:
        lines.append(f"- {late_stop_note}")
    archived_lines, stayed_lines = _disposition_lines(tasks)
    if archived_lines or stayed_lines or stop_request:
        lines.extend(["", "## Archived", ""])
        lines.extend(archived_lines or ["- (none)"])
        lines.extend(["", "## Stayed", ""])
        lines.extend(stayed_lines or ["- (none)"])
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


def _disposition_lines(tasks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    archived: list[str] = []
    stayed: list[str] = []
    ordered = sorted(tasks, key=lambda record: natural_key(str(record.get("name", ""))))
    for record in ordered:
        name = str(record.get("name", ""))
        disposition = record.get("disposition")
        if disposition == "archived":
            archived.append(f"- {name} -> {record.get('stored_at', '')}")
        elif disposition == "stayed":
            stayed.append(f"- {name} ({record.get('status', '')}) left in place")
    return archived, stayed


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
    archive: bool = True,
    archive_dir: Path | None = None,
    tasks_root: Path | None = None,
    stop_file: Path | None = None,
    run_task: RunTask | None = None,
) -> QueueResult:
    task_dir = Path(task_dir).resolve()
    project = Path(project).resolve()
    log_dir = Path(log_dir)
    if not task_dir.is_dir():
        raise FileNotFoundError(f"task folder does not exist: {task_dir}")
    ensure_repository(project, init_repo=init_repo)

    tasks_root_resolved = task_archive.resolve_tasks_root(task_dir, tasks_root)
    archive_root = Path(archive_dir).resolve() if archive_dir is not None else task_archive.default_archive_dir()
    # Exclusion uses the archive root even when moves are turned off, so a
    # folder of finished tasks is not picked up again under --no-archive.
    scan_archive = archive_root
    stop_path = Path(stop_file).resolve() if stop_file is not None else default_stop_file()

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

    discovered = list_task_files(task_dir, archive_dir=scan_archive)
    if strict:
        from local_agent.taskcheck import format_problems, validate_task_files

        problems = validate_task_files(discovered, project)
        if problems:
            raise RuntimeError(format_problems(problems))
    pending, skipped_names = pending_tasks(task_dir, state, archive_dir=scan_archive)
    if not pending and not state["tasks"]:
        raise RuntimeError(
            f"No task markdown files in {task_dir}. "
            "Add files such as 01-runner.md. README.md, queue-summary.md, "
            "and files under archive/, done/, or failed/ are ignored."
        )

    runner = run_task or _default_run_task
    stopped_on: str | None = None
    stopped_early = False
    stopped_on_request = False
    request_pending: list[str] = []
    late_stop_note = ""
    log_dir.mkdir(parents=True, exist_ok=True)

    def persist(*, in_progress: bool = False) -> None:
        save_state(state_path, state)
        if in_progress and not stopped_early and not stopped_on_request:
            status = "running"
        else:
            status = _overall_status(state, stopped_early, stopped_on_request)
        summary = render_summary(
            status=status,
            project=project,
            base_branch=base_branch,
            profile_name=profile.name,
            tasks=list(state["tasks"].values()),
            stopped_on=stopped_on,
            pending=request_pending,
            stop_request=stopped_on_request,
            late_stop_note=late_stop_note,
        )
        summary_path.write_text(summary, encoding="utf-8", newline="\n")

    # A stop file left behind by an earlier run must not cancel this one.
    # The request is created after the queue has started, from another window.
    if stop_is_requested(stop_path):
        print(
            f"warning: removing stale stop request at {stop_path}. "
            "A stop file left from an earlier run does not stop a new queue. "
            "Ask again after this queue has started "
            "(scripts/stop-queue.ps1, or python -m local_agent stop).",
            file=sys.stderr,
        )
        clear_stop_file(stop_path)

    for index, path in enumerate(pending):
        # Checked only here, before a task starts. The runner is not
        # interrupted when the file appears mid-task.
        if stop_is_requested(stop_path):
            stopped_on_request = True
            request_pending = [task_key(item, task_dir) for item in pending[index:]]
            clear_stop_file(stop_path)
            break
        key = task_key(path, task_dir)
        _git(project, "checkout", base_branch)
        task = load_task(path)
        task_log = log_dir / key.replace("/", "-")
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
            stored, disposition = _store_failure(path, task_dir, archive=archive)
            _record(
                state,
                key,
                status="error",
                branch="",
                commit=None,
                merge_commit=None,
                iterations=0,
                duration=duration,
                detail=str(exc),
                stored_at=stored,
                disposition=disposition,
            )
            if not continue_on_failure:
                stopped_on = key
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
                stored, disposition = _store_failure(path, task_dir, archive=archive)
                _record(
                    state,
                    key,
                    status="merge_failed",
                    branch=result.branch,
                    commit=result.commit,
                    merge_commit=None,
                    iterations=result.iterations,
                    duration=duration,
                    detail=str(exc),
                    stored_at=stored,
                    disposition=disposition,
                )
                stopped_on = key
                stopped_early = True
                persist(in_progress=True)
                _return_to_base(project, base_branch)
                break
            stored, disposition, detail = _store_success(
                path,
                task_dir,
                archive=archive,
                archive_root=archive_root,
                tasks_root=tasks_root_resolved,
            )
            _record(
                state,
                key,
                status="passed",
                branch=result.branch,
                commit=result.commit,
                merge_commit=merge_commit,
                iterations=result.iterations,
                duration=duration,
                detail=detail,
                stored_at=stored,
                disposition=disposition,
            )
            persist(in_progress=True)
            continue

        stored, disposition = _store_failure(path, task_dir, archive=archive)
        _record(
            state,
            key,
            status=result.status or "failed",
            branch=result.branch,
            commit=result.commit,
            merge_commit=None,
            iterations=result.iterations,
            duration=duration,
            detail="tests did not pass" if not result.tests_passed else "review rejected the change",
            stored_at=stored,
            disposition=disposition,
        )
        if not continue_on_failure:
            stopped_on = key
            stopped_early = True
        persist(in_progress=True)
        _return_to_base(project, base_branch)
        if not continue_on_failure:
            break

    # A passed file left in the task folder (move failed last time) is skipped.
    for name in skipped_names:
        source = task_dir / name
        record = state["tasks"].get(name) or {}
        if not source.is_file():
            continue
        if archive:
            if str(record.get("stored_at", "")).startswith(str(archive_root)):
                continue
            stored, disposition, detail = _store_success(
                source,
                task_dir,
                archive=True,
                archive_root=archive_root,
                tasks_root=tasks_root_resolved,
            )
            record["stored_at"] = stored
            record["disposition"] = disposition
            if detail:
                record["detail"] = detail
            state["tasks"][name] = record
            continue
        if not str(record.get("stored_at", "")).startswith(str(task_dir / "done")):
            moved = _move_task(source, task_dir / "done")
            record["stored_at"] = str(moved)
            record["disposition"] = "done"
            state["tasks"][name] = record

    if stop_is_requested(stop_path) and not stopped_on_request:
        clear_stop_file(stop_path)
        late_stop_note = (
            "A stop request arrived after the last queued task had already started. "
            "Nothing was left pending."
        )

    status = _overall_status(state, stopped_early, stopped_on_request)
    if stopped_on_request:
        pending_text = ", ".join(request_pending) if request_pending else "(none)"
        message = (
            "Stopped on request. The current task was allowed to finish. "
            f"No new task was started. Still pending: {pending_text}."
        )
    else:
        message = ""
    persist()
    records = list(state["tasks"].values())
    failed = sum(1 for record in records if record.get("status") != "passed")
    passed = sum(1 for record in records if record.get("status") == "passed")
    archived = [str(record.get("stored_at", "")) for record in records if record.get("disposition") == "archived"]
    stayed = [str(record.get("name", "")) for record in records if record.get("disposition") == "stayed"]
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
        pending=request_pending,
        archived=archived,
        stayed=stayed,
        message=message,
    )


def _default_run_task(**kwargs: Any) -> OvernightResult:
    return run_overnight(**kwargs)


def _overall_status(state: dict[str, Any], stopped_early: bool, stopped_on_request: bool) -> str:
    records = list((state.get("tasks") or {}).values())
    if stopped_on_request:
        return "stopped_on_request"
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
    disposition: str,
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
        "disposition": disposition,
    }


def _store_success(
    source: Path,
    task_dir: Path,
    *,
    archive: bool,
    archive_root: Path,
    tasks_root: Path,
) -> tuple[str, str, str]:
    """Move a passing task. Returns (stored_at, disposition, detail)."""

    if not archive:
        return str(_move_task(source, task_dir / "done")), "done", ""
    try:
        moved = task_archive.archive_task(
            source,
            archive_dir=archive_root,
            tasks_root=tasks_root,
        )
    except (OSError, ValueError) as exc:
        return str(source), "stayed", f"passed but archive failed: {exc}"
    return str(moved), "archived", ""


def _store_failure(source: Path, task_dir: Path, *, archive: bool) -> tuple[str, str]:
    """Keep a failing task in place, or move it to failed/ when archive is off."""

    if archive:
        return str(source), "stayed"
    if not source.is_file():
        return str(source), "failed"
    return str(_move_task(source, task_dir / "failed")), "failed"


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


def _scan_excluded(path: Path, archive_dir: Path | None) -> bool:
    """True when this directory must not contribute task files."""

    name = path.name.lower()
    if name.startswith(".") or name in EXCLUDED_DIR_NAMES:
        return True
    if archive_dir is None:
        return False
    resolved = path.resolve()
    return resolved == archive_dir or _is_under(resolved, archive_dir)


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent
