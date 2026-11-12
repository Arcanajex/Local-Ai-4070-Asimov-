"""The overnight loop against the scripted smoke client, and task parsing."""

from __future__ import annotations

import json
import subprocess

from local_agent.overnight import load_task, parse_task
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
