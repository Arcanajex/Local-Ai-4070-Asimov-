"""Archive layout and stop-after-the-current-task.

Passing queue files move under the archive root and keep the path relative
to the tasks root. Failing files stay put. The stop file is read only
between tasks.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from local_agent.archive import (
    archive_task,
    maybe_archive_task,
    resolve_tasks_root,
    tasks_archive_path,
)
from local_agent.cli import main
from local_agent.config import get_profile
from local_agent.overnight import OvernightResult
from local_agent.paths import repo_root
from local_agent.queue import (
    QueueResult,
    list_task_files,
    natural_key,
    run_queue,
    save_state,
)


def test_default_archive_root_is_tasks_archive_in_this_repo():
    assert tasks_archive_path(repo_root()) == repo_root() / "tasks" / "archive"


def test_tasks_root_is_the_tasks_folder_above_a_task_list(tmp_path):
    tasks = tmp_path / "tasks"
    listed = tasks / "ui"
    listed.mkdir(parents=True)
    assert resolve_tasks_root(listed) == tasks.resolve()
    assert resolve_tasks_root(repo_root() / "tasks" / "ui") == (repo_root() / "tasks").resolve()


def test_archive_mirrors_nested_folders_and_never_overwrites(tmp_path):
    tasks = tmp_path / "tasks"
    (tasks / "foo.md").parent.mkdir(parents=True)
    (tasks / "foo.md").write_text("top\n", encoding="utf-8")
    (tasks / "ui").mkdir()
    (tasks / "ui" / "step-2.md").write_text("nested\n", encoding="utf-8")
    archive = tasks / "archive"
    top = archive_task(tasks / "foo.md", archive_dir=archive, tasks_root=tasks)
    nested = archive_task(tasks / "ui" / "step-2.md", archive_dir=archive, tasks_root=tasks)
    assert top == archive / "foo.md"
    assert nested == archive / "ui" / "step-2.md"
    assert top.read_text(encoding="utf-8") == "top\n"
    assert nested.read_text(encoding="utf-8") == "nested\n"
    assert not (tasks / "foo.md").exists()
    assert not (tasks / "ui" / "step-2.md").exists()
    assert (tasks / "ui").is_dir()

    (tasks / "foo.md").write_text("again\n", encoding="utf-8")
    moment = datetime(2026, 11, 14, 1, 7, 5)
    collided = archive_task(tasks / "foo.md", archive_dir=archive, tasks_root=tasks, now=moment)
    assert collided == archive / "foo-20261114-010705.md"
    assert (archive / "foo.md").read_text(encoding="utf-8") == "top\n"
    assert collided.read_text(encoding="utf-8") == "again\n"

    (tasks / "foo.md").write_text("third\n", encoding="utf-8")
    (archive / "foo-20261114-010705.md").write_text("kept\n", encoding="utf-8")
    again = archive_task(tasks / "foo.md", archive_dir=archive, tasks_root=tasks, now=moment)
    assert again.name.startswith("foo-20261114-010705-")
    assert again.name != "foo-20261114-010705.md"
    assert (archive / "foo.md").read_text(encoding="utf-8") == "top\n"
    assert (archive / "foo-20261114-010705.md").read_text(encoding="utf-8") == "kept\n"
    assert again.read_text(encoding="utf-8") == "third\n"
    with pytest.raises(ValueError, match="already in the archive"):
        archive_task(archive / "foo.md", archive_dir=archive, tasks_root=tasks)


def test_scanner_skips_archive_even_when_it_is_nested_in_the_queue(tmp_path):
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "01-runner.md").write_text("# a\n", encoding="utf-8")
    (task_dir / "README.md").write_text("# readme\n", encoding="utf-8")
    (task_dir / "queue-summary.md").write_text("# summary\n", encoding="utf-8")
    (task_dir / "ui").mkdir()
    (task_dir / "ui" / "2-early.md").write_text("# e\n", encoding="utf-8")
    (task_dir / "ui" / "10-late.md").write_text("# l\n", encoding="utf-8")
    (task_dir / "ui" / "step-2.md").write_text("# s\n", encoding="utf-8")
    (task_dir / "archive" / "ui").mkdir(parents=True)
    (task_dir / "archive" / "foo.md").write_text("# old\n", encoding="utf-8")
    (task_dir / "archive" / "ui" / "step-2.md").write_text("# old\n", encoding="utf-8")
    (task_dir / "done").mkdir()
    (task_dir / "done" / "0-old.md").write_text("# d\n", encoding="utf-8")
    (task_dir / "failed").mkdir()
    (task_dir / "failed" / "bad.md").write_text("# f\n", encoding="utf-8")
    custom = task_dir / "outbox"
    (custom / "nested").mkdir(parents=True)
    (custom / "also.md").write_text("# a\n", encoding="utf-8")
    (custom / "nested" / "secret.md").write_text("# s\n", encoding="utf-8")
    found = [path.relative_to(task_dir).as_posix() for path in list_task_files(task_dir, archive_dir=custom)]
    assert found == ["01-runner.md", "ui/2-early.md", "ui/10-late.md", "ui/step-2.md"]
    assert natural_key("ui/2-early.md") < natural_key("ui/10-late.md")
    assert natural_key("1-c.md") < natural_key("ui/step.md")


def test_queue_archives_passes_leaves_failures_and_ignores_the_archive(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    archive = task_dir / "archive"
    (task_dir / "ui").mkdir(parents=True)
    _write(task_dir / "01-runner.md")
    _write(task_dir / "ui" / "step-2.md")
    _write(task_dir / "ui" / "step-3.md")
    _write(task_dir / "ui" / "step-9.md")
    (archive / "decoy.md").parent.mkdir(parents=True)
    (archive / "decoy.md").write_text("# not a template\n\nDo not run me.\n", encoding="utf-8")
    calls: list[str] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive_dir=archive,
        tasks_root=task_dir,
        run_task=_runner(
            {"01-runner.md": True, "step-2.md": True, "step-3.md": False, "step-9.md": True},
            calls,
        ),
    )
    assert calls == ["01-runner.md", "step-2.md", "step-3.md"]
    assert result.status == "stopped"
    assert (archive / "01-runner.md").is_file()
    assert (archive / "ui" / "step-2.md").is_file()
    assert (archive / "ui" / "step-2.md").read_text(encoding="utf-8").startswith("# ui/step-2.md")
    assert not (task_dir / "01-runner.md").exists()
    assert not (task_dir / "ui" / "step-2.md").exists()
    assert (task_dir / "ui" / "step-3.md").is_file()
    assert (task_dir / "ui" / "step-9.md").is_file()
    assert not (archive / "ui" / "step-3.md").exists()
    assert (archive / "decoy.md").is_file()
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "## Archived" in summary
    assert "01-runner.md ->" in summary
    assert "ui/step-2.md ->" in summary
    assert "## Stayed" in summary
    assert "ui/step-3.md" in summary
    assert "left in place" in summary
    assert "decoy.md" not in calls
    listed = [path.relative_to(task_dir).as_posix() for path in list_task_files(task_dir, archive_dir=archive)]
    assert "archive/decoy.md" not in listed
    assert "01-runner.md" not in listed
    assert "ui/step-2.md" not in listed
    assert listed == ["ui/step-3.md", "ui/step-9.md"]
    assert result.archived
    assert "ui/step-3.md" in result.stayed


def test_stop_file_is_honored_between_tasks_and_not_mid_task(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write(task_dir / "01-runner.md")
    _write(task_dir / "02-status.md")
    stop = tmp_path / "STOP_QUEUE"
    finished: list[str] = []
    calls: list[str] = []

    def run_task(*, task, project, profile, client, log_dir, max_seconds, max_iterations, init_repo, **_extra):
        name = task.path.name
        calls.append(name)
        stop.write_text("stop during the task\n", encoding="utf-8")
        assert stop.is_file()
        _commit_marker(project, task.path.stem, name)
        finished.append(name)
        assert stop.is_file()
        return _passed(name, log_dir)

    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive_dir=task_dir / "archive",
        tasks_root=task_dir,
        stop_file=stop,
        run_task=run_task,
    )
    assert finished == ["01-runner.md"]
    assert calls == ["01-runner.md"]
    assert not stop.exists()
    assert result.status == "stopped_on_request"
    assert result.pending == ["02-status.md"]
    assert result.message.startswith("Stopped on request")
    assert "No new task was started" in result.message
    assert (project / "01-runner.marker").is_file()
    assert _branch(project) == "integration"
    assert (task_dir / "archive" / "01-runner.md").is_file()
    assert (task_dir / "02-status.md").is_file()
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "Stopped on request:" in summary
    assert "Still pending: 02-status.md" in summary
    assert "02-status.md" not in finished


def test_stale_stop_file_is_warned_and_cleared_at_startup(tmp_path, capsys):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write(task_dir / "01-runner.md")
    _write(task_dir / "02-status.md")
    stop = tmp_path / "STOP_QUEUE"
    stop.write_text("left from yesterday\n", encoding="utf-8")
    calls: list[str] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive_dir=task_dir / "archive",
        tasks_root=task_dir,
        stop_file=stop,
        run_task=_runner({"01-runner.md": True, "02-status.md": True}, calls),
    )
    assert "stale stop request" in capsys.readouterr().err
    assert calls == ["01-runner.md", "02-status.md"]
    assert result.status == "passed"
    assert not stop.exists()
    assert (task_dir / "archive" / "01-runner.md").is_file()
    assert (task_dir / "archive" / "02-status.md").is_file()


def test_passed_file_left_behind_is_archived_on_the_next_run(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write(task_dir / "01-runner.md")
    _write(task_dir / "02-status.md")
    save_state(
        task_dir / "queue-state.json",
        {
            "version": 1,
            "base_branch": "integration",
            "tasks": {"01-runner.md": {"name": "01-runner.md", "status": "passed"}},
        },
    )
    calls: list[str] = []
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive_dir=task_dir / "archive",
        tasks_root=task_dir,
        run_task=_runner({"02-status.md": True}, calls),
    )
    assert calls == ["02-status.md"]
    assert (task_dir / "archive" / "01-runner.md").is_file()
    assert (task_dir / "archive" / "02-status.md").is_file()
    assert result.status == "passed"


def test_archive_only_markdown_is_not_a_queue(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    (task_dir / "archive").mkdir(parents=True)
    (task_dir / "archive" / "old.md").write_text("# old\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="No task markdown"):
        run_queue(
            task_dir=task_dir,
            project=project,
            profile=get_profile("day"),
            client=None,
            log_dir=tmp_path / "logs",
            archive_dir=task_dir / "archive",
            tasks_root=task_dir,
        )


def test_cli_archive_stop_and_queue_exit_codes(tmp_path, capsys, monkeypatch):
    tasks = tmp_path / "tasks"
    source = tasks / "ui" / "step-2.md"
    source.parent.mkdir(parents=True)
    source.write_text("body\n", encoding="utf-8")
    archive = tmp_path / "archive"
    code = main(
        [
            "archive",
            str(source),
            "--archive-dir",
            str(archive),
            "--tasks-root",
            str(tasks),
        ]
    )
    assert code == 0
    assert not source.exists()
    assert (archive / "ui" / "step-2.md").read_text(encoding="utf-8") == "body\n"
    assert "ui" in capsys.readouterr().out

    stop = tmp_path / "STOP_QUEUE"
    assert main(["stop", "--stop-file", str(stop)]) == 0
    assert "Stop requested" in capsys.readouterr().out
    assert stop.is_file()
    assert main(["stop", "--cancel", "--stop-file", str(stop)]) == 0
    assert not stop.exists()
    assert "withdrawn" in capsys.readouterr().out
    assert main(["stop", "--cancel", "--stop-file", str(stop)]) == 0
    assert "No stop request" in capsys.readouterr().out

    def fake_queue(**_kwargs):
        return QueueResult(
            status="stopped_on_request",
            base_branch="integration",
            passed=1,
            failed=0,
            skipped=0,
            stopped_on=None,
            summary_path=tmp_path / "queue-summary.md",
            state_path=tmp_path / "queue-state.json",
            pending=["02-status.md"],
            message=(
                "Stopped on request. The current task was allowed to finish. "
                "No new task was started. Still pending: 02-status.md."
            ),
        )

    monkeypatch.setattr("local_agent.cli.run_queue", fake_queue)
    code = main(["queue", "--task-dir", str(tmp_path), "--project", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err.startswith("Stopped on request")
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["status"] == "stopped_on_request"
    assert payload["pending"] == ["02-status.md"]

    def fake_failed(**_kwargs):
        return QueueResult(
            status="stopped",
            base_branch="integration",
            passed=0,
            failed=1,
            skipped=0,
            stopped_on="01-runner.md",
            summary_path=tmp_path / "queue-summary.md",
            state_path=tmp_path / "queue-state.json",
        )

    monkeypatch.setattr("local_agent.cli.run_queue", fake_failed)
    assert main(["queue", "--task-dir", str(tmp_path), "--project", str(tmp_path)]) == 1
    assert (
        main(
            [
                "queue",
                "--task-dir",
                str(tmp_path),
                "--project",
                str(tmp_path),
                "--no-archive",
                "--archive-dir",
                str(tmp_path / "archive"),
            ]
        )
        == 2
    )
    assert "cannot be used together" in capsys.readouterr().err


def test_overnight_archives_only_after_a_passing_run(tmp_path, monkeypatch, capsys):
    task = tmp_path / "tasks" / "ui" / "step-2.md"
    task.parent.mkdir(parents=True)
    task.write_text("# Step\n\nDo the thing.\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(**_kwargs):
        return OvernightResult(
            status=seen.get("status", "passed"),
            branch="agent/step",
            report_path=None,
            commit="abc",
            iterations=1,
            tests_passed=seen.get("status", "passed") == "passed",
            log_dir=tmp_path / "logs",
            review_passed=True,
        )

    def fake_archive(source, **kwargs):
        seen["source"] = Path(source)
        seen["archive_dir"] = kwargs.get("archive_dir")
        seen["tasks_root"] = kwargs.get("tasks_root")
        return tmp_path / "archive" / "ui" / "step-2.md"

    monkeypatch.setattr("local_agent.cli.run_overnight", fake_run)
    monkeypatch.setattr("local_agent.archive.archive_task", fake_archive)
    code = main(
        [
            "overnight",
            "--task",
            str(task),
            "--project",
            str(tmp_path),
            "--no-strict",
            "--archive-dir",
            str(tmp_path / "archive"),
            "--tasks-root",
            str(tmp_path / "tasks"),
        ]
    )
    assert code == 0
    assert seen["source"] == task
    assert seen["tasks_root"] == tmp_path / "tasks"
    assert "Archived" in capsys.readouterr().err

    seen.clear()
    seen["status"] = "tests_failed"
    code = main(
        [
            "overnight",
            "--task",
            str(task),
            "--project",
            str(tmp_path),
            "--no-strict",
        ]
    )
    assert code == 1
    assert "source" not in seen
    assert task.is_file()

    seen.clear()
    seen["status"] = "passed"
    code = main(
        [
            "overnight",
            "--task",
            str(task),
            "--project",
            str(tmp_path),
            "--no-strict",
            "--no-archive",
        ]
    )
    assert code == 0
    assert "source" not in seen
    assert task.is_file()
    assert maybe_archive_task(task, enabled=True, succeeded=False) is None


def _runner(outcomes: dict[str, bool], calls: list[str]):
    def run_task(*, task, project, profile, client, log_dir, max_seconds, max_iterations, init_repo, **_extra):
        name = task.path.name
        calls.append(name)
        passed = outcomes[name]
        if passed:
            _commit_marker(project, task.path.stem, name)
        return _passed(name, log_dir) if passed else _failed(name, log_dir)

    return run_task


def _passed(name: str, log_dir: Path) -> OvernightResult:
    return OvernightResult(
        status="passed",
        branch=f"agent/{Path(name).stem}",
        report_path=None,
        commit="abc",
        iterations=1,
        tests_passed=True,
        log_dir=log_dir,
    )


def _failed(name: str, log_dir: Path) -> OvernightResult:
    return OvernightResult(
        status="tests_failed",
        branch="",
        report_path=None,
        commit=None,
        iterations=2,
        tests_passed=False,
        log_dir=log_dir,
        review_passed=False,
    )


def _commit_marker(project: Path, stem: str, name: str) -> None:
    branch = f"agent/{stem}"
    subprocess.run(["git", "checkout", "-b", branch], cwd=project, check=True, stdout=subprocess.DEVNULL)
    (project / f"{stem}.marker").write_text(name + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Queue Test", "-c", "user.email=queue@example.com", "commit", "-m", name],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _project(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "integration"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Queue Test", "-c", "user.email=queue@example.com", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return path


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.name if path.parent.name == "tasks" else f"{path.parent.name}/{path.name}"
    path.write_text(
        f"# {title}\n\n"
        "## Goal\n\nDo the work.\n\n"
        "## Acceptance criteria\n\n- [ ] marker file exists\n\n"
        "## Out of scope\n\n- Do not edit unrelated files.\n",
        encoding="utf-8",
    )


def _branch(project: Path) -> str:
    return subprocess.check_output(["git", "branch", "--show-current"], cwd=project, text=True).strip()
