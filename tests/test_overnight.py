"""The overnight loop against the scripted smoke client, and task parsing."""

from __future__ import annotations

import json
import subprocess

import pytest

from local_agent.overnight import (
    ensure_repository,
    load_task,
    parse_task,
    working_tree_diff,
)
from local_agent.paths import repo_root
from local_agent.smoke import run_mock_smoke


def test_task_front_matter_and_title():
    task = load_task(repo_root() / "tasks" / "smoke-task.md")
    assert task.title == "Smoke task: adder"
    assert task.test_command == "python -m pytest -q"
    assert task.lint_command is None
    bare = parse_task("# Just a title\n\nDo the thing.\n")
    assert bare.title == "Just a title"
    assert bare.test_command == "python -m pytest -q"


def test_mock_smoke_fixes_a_bug_and_commits():
    result = run_mock_smoke()
    assert result.tests_passed, result.report_path.read_text(encoding="utf-8") if result.report_path else result
    assert result.status == "passed"
    assert result.commit
    assert result.branch.startswith("agent/smoke-task-adder-")
    project = result.report_path.parent
    adder = (project / "adder.py").read_text(encoding="utf-8")
    assert "return a + b" in adder
    assert "return a - b" not in adder
    report = result.report_path.read_text(encoding="utf-8")
    assert "Verdict: accept" in report
    assert "Status: passed" in report
    log = (result.log_dir / "events.jsonl").read_text(encoding="utf-8")
    kinds = [json.loads(line)["kind"] for line in log.splitlines()]
    assert "request" in kinds
    assert "tool_result" in kinds
    assert "tests" in kinds
    assert "phase_start" in kinds
    subject = subprocess.check_output(
        ["git", "log", "-1", "--format=%s"], cwd=project, text=True
    ).strip()
    assert subject.startswith("agent:")
    assert "passed" in subject
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=project, text=True
    )
    for name in ("adder.py", "test_adder.py", "AGENT_PLAN.md", "AGENT_REPORT.md"):
        assert name in files
    review_prompt = _review_request(log)
    # These files are created during the run and are still untracked when the
    # review prompt is built. `git diff` alone does not show them.
    assert "adder.py" in review_prompt
    assert "test_adder.py" in review_prompt
    assert "return a + b" in review_prompt


def test_diff_includes_new_files_that_git_diff_skips(tmp_path):
    """An empty initial commit plus brand-new files is the Legion failure."""

    project = tmp_path / "localRepo"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "-c", "user.email=t@example.com", "-c", "user.name=Tester", "commit", "--allow-empty", "-m", "Initial commit")
    (project / "textstats.py").write_text(
        "def count_words(text):\n    return len(text.split())\n", encoding="utf-8"
    )
    (project / "test_textstats.py").write_text(
        "from textstats import count_words\n\ndef test_count():\n    assert count_words('a b') == 2\n",
        encoding="utf-8",
    )
    plain = subprocess.check_output(["git", "diff"], cwd=project, text=True)
    assert plain.strip() == ""
    diff = working_tree_diff(project)
    assert "textstats.py" in diff
    assert "test_textstats.py" in diff
    assert "def count_words" in diff
    assert "def test_count" in diff
    assert "+++ b/textstats.py" in diff
    assert "+++ b/test_textstats.py" in diff


def test_init_repo_switch_and_missing_identity(tmp_path, monkeypatch, capsys):
    project = tmp_path / "localRepo"
    project.mkdir()
    with pytest.raises(RuntimeError, match="not a git repository") as raised:
        ensure_repository(project, init_repo=False)
    message = str(raised.value)
    assert "--init-repo" in message
    assert "-InitRepo" in message

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    note = ensure_repository(project, init_repo=True)
    assert note is not None
    assert "user.name" in note
    assert "user.email" in note
    assert "Local Coding Agent" in note
    assert "git config was not changed" in note or "without changing your git config" in note
    log = subprocess.check_output(["git", "log", "--oneline"], cwd=project, text=True)
    assert "Initial commit" in log
    again = ensure_repository(project, init_repo=True)
    assert again == note
    count = subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=project, text=True).strip()
    assert count == "1"
    # The helper itself does not print; the runner does. Nothing should have leaked here.
    assert capsys.readouterr().err == ""


def _review_request(log: str) -> str:
    for line in log.splitlines():
        event = json.loads(line)
        if event.get("kind") != "request":
            continue
        for message in event.get("messages") or []:
            content = message.get("content") or ""
            if "Review the diff" in content:
                return content
    raise AssertionError("review request was not logged")


def _git(project, *args: str) -> None:
    subprocess.run(["git", *args], cwd=project, check=True, stdout=subprocess.DEVNULL)
