"""Queue ordering, resume, merge-on-pass, and stop/continue policy.

The overnight model is replaced with a fake that commits on a new branch.
Merging those branches is real git, so a passing task is actually on the
branch the next task starts from.

These tests pass archive=False so they keep pinning the done/ and failed/
layout. Archiving and the stop file are covered in test_archive_and_stop.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_agent.config import get_profile
from local_agent.overnight import OvernightResult
from local_agent.queue import (
    list_task_files,
    load_state,
    natural_key,
    pending_tasks,
    render_summary,
    run_queue,
    save_state,
)


def test_natural_sort_orders_numbered_task_files(tmp_path):
    for name in ("10-b.md", "2-a.md", "1-c.md", "README.md", "queue-summary.md"):
        (tmp_path / name).write_text("# task\n", encoding="utf-8")
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / "0-old.md").write_text("# old\n", encoding="utf-8")
    names = [path.name for path in list_task_files(tmp_path)]
    assert names == ["1-c.md", "2-a.md", "10-b.md"]
    assert natural_key("02-status.md") < natural_key("10-notify.md")


def test_pending_skips_tasks_already_passed(tmp_path):
    (tmp_path / "01-runner.md").write_text("# one\n", encoding="utf-8")
    (tmp_path / "02-status.md").write_text("# two\n", encoding="utf-8")
    state = {
        "version": 1,
        "tasks": {"01-runner.md": {"name": "01-runner.md", "status": "passed"}},
    }
    pending, skipped = pending_tasks(tmp_path, state)
    assert [path.name for path in pending] == ["02-status.md"]
    assert skipped == ["01-runner.md"]


def test_merge_on_pass_then_resume_and_summary(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write_tasks(task_dir, ["01-runner.md", "02-status.md"])
    calls: list[str] = []
    starts: list[tuple[str, str]] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        run_task=_runner({"01-runner.md": True, "02-status.md": True}, calls, starts),
    )
    assert result.status == "passed"
    assert calls == ["01-runner.md", "02-status.md"]
    assert (task_dir / "done" / "01-runner.md").is_file()
    assert (task_dir / "done" / "02-status.md").is_file()
    assert not (task_dir / "01-runner.md").exists()
    assert _branch(project) == "integration"
    assert (project / "01-runner.marker").read_text(encoding="utf-8") == "01-runner.md\n"
    assert (project / "02-status.marker").is_file()
    assert _is_merge_commit(project)
    # The second task started from the first task's merge, not from the empty root.
    assert starts[1][1] != starts[0][1]
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "01-runner.md" in summary
    assert "02-status.md" in summary
    assert "passed" in summary
    state = load_state(result.state_path)
    assert state["tasks"]["01-runner.md"]["status"] == "passed"
    assert state["tasks"]["01-runner.md"]["merge_commit"]

    # Re-running with a new file does not repeat the tasks that already passed.
    _write_tasks(task_dir, ["03-window.md"])
    calls.clear()
    again = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        run_task=_runner({"03-window.md": True}, calls, starts),
    )
    assert again.skipped >= 2
    assert calls == ["03-window.md"]
    assert again.status == "passed"


def test_stop_on_failure_does_not_start_later_tasks(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write_tasks(task_dir, ["01-runner.md", "02-status.md", "03-window.md"])
    calls: list[str] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        run_task=_runner(
            {"01-runner.md": True, "02-status.md": False, "03-window.md": True},
            calls,
            [],
        ),
    )
    assert result.status == "stopped"
    assert result.stopped_on == "02-status.md"
    assert calls == ["01-runner.md", "02-status.md"]
    assert (task_dir / "03-window.md").is_file()
    assert (task_dir / "failed" / "02-status.md").is_file()
    assert (project / "01-runner.marker").is_file()
    assert not (project / "02-status.marker").exists()
    assert _branch(project) == "integration"
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "Stopped on: 02-status.md" in summary


def test_continue_on_failure_skips_the_failed_merge_and_runs_the_rest(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write_tasks(task_dir, ["01-runner.md", "02-status.md", "03-window.md"])
    calls: list[str] = []
    starts: list[tuple[str, str]] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        continue_on_failure=True,
        run_task=_runner(
            {"01-runner.md": True, "02-status.md": False, "03-window.md": True},
            calls,
            starts,
        ),
    )
    assert result.status == "completed_with_failures"
    assert calls == ["01-runner.md", "02-status.md", "03-window.md"]
    assert (project / "01-runner.marker").is_file()
    assert (project / "03-window.marker").is_file()
    assert not (project / "02-status.marker").exists()
    # Task 3 forked from main after task 1's merge, not from the failed branch.
    merge_of_first = starts[2][1]
    assert _is_ancestor(project, "agent/02-status", merge_of_first) is False
    assert (task_dir / "failed" / "02-status.md").is_file()
    assert (task_dir / "done" / "03-window.md").is_file()


def test_summary_lists_every_recorded_task():
    text = render_summary(
        status="stopped",
        project=Path("/work/app"),
        base_branch="main",
        profile_name="quality",
        stopped_on="02-status.md",
        tasks=[
            {
                "name": "02-status.md",
                "status": "tests_failed",
                "branch": "agent/status",
                "commit": "abc",
                "iterations": 3,
                "duration_seconds": 12.5,
            },
            {
                "name": "01-runner.md",
                "status": "passed",
                "branch": "agent/runner",
                "commit": "def",
                "iterations": 1,
                "duration_seconds": 4,
            },
        ],
    )
    rows = [line for line in text.splitlines() if line.startswith("| 0")]
    assert rows[0].startswith("| 01-runner.md")
    assert rows[1].startswith("| 02-status.md")
    assert "Stopped on: 02-status.md" in text
    assert "quality" in text
    assert "4.0s" in text
    assert "12.5s" in text


def test_passed_file_left_in_the_folder_is_not_run_again(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write_tasks(task_dir, ["01-runner.md", "02-status.md"])
    save_state(
        task_dir / "queue-state.json",
        {
            "version": 1,
            "base_branch": "integration",
            "tasks": {
                "01-runner.md": {
                    "name": "01-runner.md",
                    "status": "passed",
                    "branch": "agent/old",
                    "commit": "abc",
                    "iterations": 1,
                    "duration_seconds": 1,
                }
            },
        },
    )
    calls: list[str] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        run_task=_runner({"02-status.md": True}, calls, []),
    )
    assert calls == ["02-status.md"]
    assert (task_dir / "done" / "01-runner.md").is_file()
    assert result.status == "passed"


def test_queue_rejects_an_empty_folder(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    with pytest.raises(RuntimeError, match="No task markdown"):
        run_queue(
            task_dir=task_dir,
            project=project,
            profile=get_profile("day"),
            client=None,
            log_dir=tmp_path / "logs",
        )


def _runner(outcomes: dict[str, bool], calls: list[str], starts: list[tuple[str, str]]):
    def run_task(*, task, project, profile, client, log_dir, max_seconds, max_iterations, init_repo, **_extra):
        name = task.path.name
        calls.append(name)
        starts.append((name, _rev_parse(project)))
        branch = f"agent/{task.path.stem}"
        subprocess.run(["git", "checkout", "-b", branch], cwd=project, check=True)
        (project / f"{task.path.stem}.marker").write_text(name + "\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Queue Test", "-c", "user.email=queue@example.com", "commit", "-m", name],
            cwd=project,
            check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=project, text=True
        ).strip()
        passed = outcomes[name]
        return OvernightResult(
            status="passed" if passed else "tests_failed",
            branch=branch,
            report_path=None,
            commit=sha,
            iterations=1 if passed else 3,
            tests_passed=passed,
            log_dir=log_dir,
        )

    return run_task


def _project(path: Path, branch: str = "integration") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", branch], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Queue Test", "-c", "user.email=queue@example.com", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
    )
    return path


def _write_tasks(task_dir: Path, names: list[str]) -> None:
    for name in names:
        (task_dir / name).write_text(
            f"# {name}\n\n"
            "## Goal\n\nDo the work.\n\n"
            "## Acceptance criteria\n\n- [ ] marker file exists\n\n"
            "## Out of scope\n\n- Do not edit unrelated files.\n",
            encoding="utf-8",
        )


def _branch(project: Path) -> str:
    return subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=project, text=True
    ).strip()


def _rev_parse(project: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()


def _is_merge_commit(project: Path) -> bool:
    line = subprocess.check_output(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=project, text=True
    ).split()
    return len(line) >= 3


def _is_ancestor(project: Path, ancestor_ref: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor_ref, descendant],
        cwd=project,
        check=False,
    )
    return result.returncode == 0
