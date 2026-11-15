"""Smoke gate, test-first locks, retries, repo map, and the eval plumbing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from local_agent.cli import main
from local_agent.client import ChatResult, ToolCall
from local_agent.config import get_profile
from local_agent.evalset import discover, run_eval
from local_agent.harness import AgentHarness
from local_agent.logging_util import RunLogger
from local_agent.overnight import (
    apply_project_defaults,
    load_task,
    parse_task,
    run_overnight,
    run_with_retries,
)
from local_agent.repo_map import build_repo_map
from local_agent.smoke_gate import run_smoke
from local_agent.taskcheck import review_problems, validate_task, validate_task_warnings
from local_agent.test_first import classify_test_run, is_test_path
from local_agent.tools import Workspace
from local_agent.traceback_files import format_traceback_files


def test_is_test_path_and_failure_classification():
    assert is_test_path("test_app.py")
    assert is_test_path("tests/test_app.py")
    assert is_test_path("pkg/foo_test.py")
    assert is_test_path("web/app.test.ts")
    assert not is_test_path("src/app.py")
    assert not is_test_path("notes.md")
    assert classify_test_run("ok", True) == "passed_early"
    assert classify_test_run("E SyntaxError: invalid syntax", False) == "broken"
    assert classify_test_run("E AssertionError: 4 != 5", False) == "expected_fail"


def test_smoke_run_fails_on_exit_and_traceback_and_launch_stays_up(tmp_path):
    (tmp_path / "crash.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    crash = run_smoke(f'"{sys.executable}" crash.py', cwd=tmp_path, mode="run", timeout=15)
    assert crash.ran and not crash.ok and crash.reason == "exit"

    (tmp_path / "tb.py").write_text(
        "import traceback\ntry:\n    raise RuntimeError('x')\nexcept Exception:\n    traceback.print_exc()\n",
        encoding="utf-8",
    )
    quiet = run_smoke(f'"{sys.executable}" tb.py', cwd=tmp_path, mode="run", timeout=15)
    assert not quiet.ok and quiet.reason == "traceback"

    (tmp_path / "sleep.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    held = run_smoke(f'"{sys.executable}" sleep.py', cwd=tmp_path, mode="launch", launch_seconds=1)
    assert held.ok and held.reason == "passed"

    (tmp_path / "early.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    early = run_smoke(f'"{sys.executable}" early.py', cwd=tmp_path, mode="launch", launch_seconds=3)
    assert not early.ok and early.reason == "exited_early"

    skipped = run_smoke("", cwd=tmp_path)
    assert skipped.ran is False and skipped.ok


def test_launch_traceback_fails_even_if_the_process_keeps_running(tmp_path):
    (tmp_path / "boom.py").write_text(
        "import time\nimport traceback\n"
        "try:\n    raise RuntimeError('boom')\nexcept Exception:\n    traceback.print_exc()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    result = run_smoke(f'"{sys.executable}" boom.py', cwd=tmp_path, mode="launch", launch_seconds=2)
    assert not result.ok
    assert result.reason == "traceback"
    assert "boom" in result.output


def test_smoke_failure_is_handed_to_the_fix_phase(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "app.py").write_text("print('up')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "app"],
        cwd=project,
        check=True,
    )
    task = parse_task(
        "---\n"
        "test_command: true\n"
        "smoke_command: false\n"
        "smoke_mode: run\n"
        "---\n"
        "# Launch\n\n## Goal\n\nRun.\n\n## Acceptance criteria\n\n- [ ] it stays up\n\n## Out of scope\n\n- nothing\n",
        path=tmp_path / "task.md",
    )
    seen: list[str] = []

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            seen.append(prompt)
            if "Review the diff" in prompt:
                return _text(_accept())
            return _text("done")

    result = run_overnight(
        task=task,
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=1,
    )
    assert result.tests_passed
    assert result.smoke_passed is False
    assert result.status == "iteration_budget"
    assert any("Smoke check failed" in prompt or "smoke check failed" in prompt for prompt in seen)
    report = result.report_path.read_text(encoding="utf-8")
    assert "Smoke passed: no" in report


def test_agents_md_supplies_the_smoke_command(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "AGENTS.md").write_text(
        "---\nsmoke_command: python app.py\nsmoke_mode: launch\nsmoke_launch_seconds: 5\n---\n\nBe careful.\n",
        encoding="utf-8",
    )
    task = parse_task("# Title\n\n## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] y\n\n## Out of scope\n\n- z\n")
    apply_project_defaults(task, project)
    assert task.smoke_command == "python app.py"
    assert task.smoke_mode == "launch"
    assert task.smoke_launch_seconds == 5.0
    explicit = parse_task("---\nsmoke_command: python other.py\n---\n# T\n\n## Goal\n\nX\n\n## Acceptance criteria\n\n- y\n\n## Out of scope\n\n- z\n")
    apply_project_defaults(explicit, project)
    assert explicit.smoke_command == "python other.py"


def test_gui_task_without_smoke_warns_and_does_not_fail_validation(tmp_path):
    project = _repo(tmp_path / "repo")
    task = parse_task(
        "# Window\n\n## Goal\n\nA tkinter window.\n\n## Acceptance criteria\n\n- [ ] it opens\n\n## Out of scope\n\n- none\n"
    )
    assert validate_task(task, project) == []
    warnings = validate_task_warnings(task, project)
    assert warnings and "smoke_command" in warnings[0]
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "# Window\n\n## Goal\n\nA tkinter window.\n\n## Acceptance criteria\n\n- [ ] it opens\n\n## Out of scope\n\n- none\n",
        encoding="utf-8",
    )
    assert main(["validate", str(task_path), "--project", str(project)]) == 0


def test_test_first_locks_failing_tests_and_rejects_edits(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "calc"],
        cwd=project,
        check=True,
    )
    task = parse_task(
        "---\ntest_first: true\ntest_command: python -m pytest -q\n---\n"
        "# Sum\n\n## Goal\n\nadd returns the sum.\n\n## Acceptance criteria\n\n- [ ] add(2, 3) is 5\n\n## Out of scope\n\n- no CLI\n"
    )
    state = {"phase": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            if "Write tests that encode" in prompt:
                return _call(
                    "write",
                    {
                        "path": "test_calc.py",
                        "content": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                    },
                )
            if "Do not implement the feature" in prompt and "did not collect" in prompt:
                return _text("tests are fine")
            if "Implement the task." in prompt:
                state["phase"] += 1
                if state["phase"] == 1:
                    return _call(
                        "edit",
                        {"path": "test_calc.py", "old_string": "assert add(2, 3) == 5", "new_string": "assert True"},
                    )
                return _call(
                    "edit",
                    {"path": "calc.py", "old_string": "return 0", "new_string": "return a + b"},
                )
            if "Review the diff" in prompt:
                return _text(
                    "Acceptance criteria:\n- add(2, 3) is 5: met\n"
                    "Out-of-scope files touched:\nnone\n"
                    "Conflicts with AGENTS.md or decisions:\nnone\n"
                    "Tests vs criteria:\nThe locked test calls add.\nCoverage: ok\n"
                    "Verdict: accept\n"
                )
            if "The tests failed" in prompt:
                return _text("already fixed")
            return _text("plan: edit calc.py")

    result = run_overnight(
        task=task,
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=3,
        max_seconds=60,
        max_plan_turns=2,
        max_work_turns=4,
        test_command=f"{sys.executable} -m pytest -q",
    )
    assert result.status == "passed", result.report_path.read_text(encoding="utf-8")
    text = (project / "test_calc.py").read_text(encoding="utf-8")
    assert "assert add(2, 3) == 5" in text
    assert "return a + b" in (project / "calc.py").read_text(encoding="utf-8")
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "test_first_lock" in log


def test_test_first_early_pass_warns_and_continues(tmp_path, capsys):
    project = _repo(tmp_path / "repo")
    (project / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "calc.py"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "calc"],
        cwd=project,
        check=True,
    )
    task = parse_task(
        "---\ntest_command: python -m pytest -q\n---\n"
        "# Sum\n\n## Goal\n\nadd exists.\n\n## Acceptance criteria\n\n- [ ] add(2, 3) is 5\n\n## Out of scope\n\n- none\n"
    )

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            if "Write tests that encode" in prompt:
                return _call(
                    "write",
                    {
                        "path": "test_calc.py",
                        "content": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                    },
                )
            if "Review the diff" in prompt:
                return _text(
                    "Acceptance criteria:\n- add(2, 3) is 5: met\n"
                    "Out-of-scope files touched:\nnone\n"
                    "Conflicts with AGENTS.md or decisions:\nnone\n"
                    "Tests vs criteria:\ncovered\nCoverage: ok\n"
                    "Verdict: accept\n"
                )
            return _text("nothing to change")

    result = run_overnight(
        task=task,
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=3,
        test_first=True,
        test_command=f"{sys.executable} -m pytest -q",
    )
    assert result.status == "passed"
    assert "passed before the implementation" in capsys.readouterr().err


def test_shell_cannot_delete_a_locked_test(tmp_path):
    (tmp_path / "test_keep.py").write_text("def test_keep():\n    assert True\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    workspace.lock_files(["test_keep.py"])
    profile = _day()
    profile.reasoning_replay_turns = 0
    client = _Script([
        _call("shell", {"command": "rm test_keep.py", "timeout_seconds": 5}),
        _text("stopped"),
    ])
    harness = AgentHarness(client, workspace, RunLogger(tmp_path / "logs"), profile)
    result = harness.run("remove the test", max_turns=3)
    assert result.status == "completed"
    assert (tmp_path / "test_keep.py").is_file()
    assert "restored locked" in client.calls[1]["messages"][-1]["content"]


def test_retry_starts_r2_with_the_failure_and_stop_file_blocks_it(tmp_path):
    project = _repo(tmp_path / "repo")
    _commit_check(project)
    task = parse_task(
        f"---\ntest_command: {sys.executable} check.py\n---\n"
        "# Marker\n\n## Goal\n\nCreate ok.txt.\n\n## Acceptance criteria\n\n- [ ] ok.txt exists\n\n## Out of scope\n\n- none\n"
    )

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            if "Review the diff" in prompt:
                return _text(_accept())
            if "Previous attempt" in prompt and "Implement the task." in prompt:
                return _call("write", {"path": "ok.txt", "content": "ok\n"})
            return _text("plan only")

    result = run_with_retries(
        retries=1,
        task=task,
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    assert result.status == "passed"
    assert result.attempts == 2
    assert result.branch.endswith("-r2") or "-r2-" in result.branch
    assert result.attempt_history[0]["status"] == "iteration_budget"
    assert (project / "ok.txt").is_file()
    branches = subprocess.check_output(["git", "branch"], cwd=project, text=True)
    assert "-r1" in branches

    project2 = _repo(tmp_path / "repo2")
    _commit_check(project2)
    stop = tmp_path / "STOP"
    stop.write_text("stop\n", encoding="utf-8")
    calls = {"n": 0}

    class Counting:
        def chat(self, messages, *, tools, max_tokens):
            calls["n"] += 1
            return _text(_accept() if "Review the diff" in _first(messages) else "no")

    stopped = run_with_retries(
        retries=1,
        stop_file=stop,
        task=task,
        project=project2,
        profile=_day(),
        client=Counting(),
        log_dir=tmp_path / "logs2",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=1,
    )
    assert stopped.stop_requested
    assert stopped.attempts == 1
    assert subprocess.check_output(["git", "branch", "--show-current"], cwd=project2, text=True).strip() != stopped.branch


def test_time_budget_is_not_retried(tmp_path):
    project = _repo(tmp_path / "repo")
    task = load_task_body()
    calls = {"n": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            calls["n"] += 1
            return _text("later")

    result = run_with_retries(
        retries=2,
        task=task,
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=0,
        max_plan_turns=2,
        max_work_turns=2,
    )
    assert result.status == "time_budget"
    assert result.attempts == 1


def test_repo_map_and_traceback_files(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text(
        "class Box:\n    def area(self):\n        return 1\n\ndef total():\n    return 0\n",
        encoding="utf-8",
    )
    mapped = build_repo_map(tmp_path, limit_chars=2000)
    assert "pkg/calc.py" in mapped
    assert "class Box" in mapped
    assert "total()" in mapped
    output = 'File "pkg/calc.py", line 4, in total\nFile "/usr/lib/python3.12/pathlib.py", line 1\n'
    cited = format_traceback_files(tmp_path, output)
    assert "pkg/calc.py" in cited
    assert "pathlib.py" not in cited
    assert "def total" in cited


def test_review_coverage_gap_fails_only_when_required():
    text = (
        "Acceptance criteria:\n- it works: met\n"
        "Out-of-scope files touched:\nnone\n"
        "Conflicts with AGENTS.md or decisions:\nnone\n"
        "Tests vs criteria:\nmissing the second criterion\nCoverage: gap\n"
        "Verdict: accept\n"
    )
    assert review_problems(text) == []
    assert any("coverage" in item for item in review_problems(text, require_test_coverage=True))


def _commit_check(project: Path) -> None:
    (project / "check.py").write_text(
        "import pathlib\nimport sys\nsys.exit(0 if pathlib.Path('ok.txt').is_file() else 1)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "check.py"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "check"],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def test_eval_mock_scoreboard(tmp_path):
    names = {fixture.name for fixture in discover()}
    assert names >= {
        "tkinter-launch",
        "new-file",
        "bugfix-test",
        "refactor-scope",
        "shell-script",
        "cli-exit",
        "two-criteria",
        "off-by-one",
        "default-on",
    }
    board = run_eval(mock=True, output_dir=tmp_path)
    assert board.ok, board.summary()
    assert len(board.results) == 9
    assert board.path is not None and board.path.is_file()
    saved = json.loads(board.path.read_text(encoding="utf-8"))
    assert saved["mode"] == "mock"
    assert saved["passed"] == 9
    code = main(["eval", "--mock", "--only", "default-on", "--output-dir", str(tmp_path / "cli")])
    assert code == 0


def _day():
    profile = get_profile("day")
    profile.context = 8192
    profile.agent_max_tokens = 256
    profile.agent_max_tokens_ceiling = 512
    return profile


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return path


def _text(content: str) -> ChatResult:
    return ChatResult(content=content, reasoning="", tool_calls=[], finish_reason="stop")


def _call(name: str, arguments: dict) -> ChatResult:
    return ChatResult(
        content="",
        reasoning="",
        tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _accept() -> str:
    return (
        "Acceptance criteria:\n- it works: met\n"
        "Out-of-scope files touched:\nnone\n"
        "Conflicts with AGENTS.md or decisions:\nnone\n"
        "Verdict: accept\n"
    )


def _first(messages) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def load_task_body():
    return parse_task(
        "---\ntest_command: true\n---\n# Wait\n\n## Goal\n\nNothing.\n\n## Acceptance criteria\n\n- [ ] done\n\n## Out of scope\n\n- none\n"
    )


class _Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, tools, max_tokens):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        return self.replies.pop(0)
