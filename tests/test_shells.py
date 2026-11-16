"""Platform-shell command strings, and the shell-script eval skip."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from local_agent.evalset import Scoreboard
from local_agent.overnight import parse_task
from local_agent.paths import repo_root
from local_agent.shells import (
    SHELL_SKIP_CODE,
    SHELL_SKIP_STATUS,
    command_fails,
    command_succeeds,
    find_posix_shell,
    format_shell_command,
    python_shell,
)
from local_agent.smoke_gate import run_smoke
from local_agent.tools import run_command


def test_front_matter_keeps_quotes_inside_a_shell_command(monkeypatch):
    import local_agent.shells as shells

    command = python_shell("-m", "pytest", "-q")
    task = parse_task(f"---\ntest_command: {command}\n---\n# t\n")
    assert task.test_command == command
    monkeypatch.setattr(shells.os, "name", "posix")
    monkeypatch.setattr(shells.sys, "executable", "/usr/bin/my python")
    quoted = shells.python_shell("-m", "pytest", "-q")
    assert quoted.startswith("'/usr/bin/my python'")
    kept = parse_task(f"---\ntest_command: {quoted}\n---\n# t\n")
    assert kept.test_command == quoted
    monkeypatch.setattr(shells.os, "name", "nt")
    monkeypatch.setattr(shells.sys, "executable", r"C:\Program Files\runtime\venv\Scripts\python.exe")
    windows = shells.python_shell("-m", "pytest", "-q")
    assert windows.startswith("& ")
    kept_windows = parse_task(f"---\ntest_command: {windows}\n---\n# t\n")
    assert kept_windows.test_command == windows
    wrapped = parse_task('---\ntest_command: "npm test"\n---\n# t\n')
    assert wrapped.test_command == "npm test"
    single = parse_task("---\nsmoke_command: 'python app.py'\n---\n# t\n")
    assert single.smoke_command == "python app.py"


def test_powershell_quotes_a_path_with_the_call_operator(monkeypatch):
    import local_agent.shells as shells

    monkeypatch.setattr(shells.os, "name", "nt")
    monkeypatch.setattr(shells.sys, "executable", r"C:\Program Files\runtime\venv\Scripts\python.exe")
    command = shells.python_shell("crash.py")
    assert command == "& 'C:\\Program Files\\runtime\\venv\\Scripts\\python.exe' 'crash.py'"
    quoted = shells.format_shell_command(["git", "commit", "-m", "add feature"])
    assert quoted.startswith("& ")
    assert "'add feature'" in quoted


def test_bash_quotes_without_a_call_operator(monkeypatch):
    import local_agent.shells as shells

    monkeypatch.setattr(shells.os, "name", "posix")
    monkeypatch.setattr(shells.sys, "executable", "/usr/bin/python3")
    command = shells.python_shell("-m", "pytest", "-q")
    assert not command.startswith("&")
    assert command == "/usr/bin/python3 -m pytest -q"
    assert format_shell_command(["echo", "hello"]) == "echo hello"
    spaced = shells.format_shell_command(["/usr/bin/my python", "-c", "print('ok')"])
    assert spaced.startswith("'/usr/bin/my python'")
    assert "print(" in spaced


def test_success_and_python_commands_run_in_the_platform_shell(tmp_path):
    ran = run_command(python_shell("-c", "print('shell-ok')"), cwd=tmp_path, timeout=30)
    assert ran.ok, ran.output
    assert "shell-ok" in ran.output
    ok = run_command(command_succeeds(), cwd=tmp_path, timeout=30)
    assert ok.ok, ok.output
    failed = run_command(command_fails(), cwd=tmp_path, timeout=30)
    assert not failed.ok


def test_smoke_runs_this_interpreter(tmp_path):
    (tmp_path / "ok.py").write_text("print('smoke-ok')\n", encoding="utf-8")
    result = run_smoke(python_shell("ok.py"), cwd=tmp_path, mode="run", timeout=30)
    assert result.ok and result.reason == "passed", result.output
    assert "smoke-ok" in result.output


def test_posix_shell_lookup_matches_this_machine():
    shell = find_posix_shell()
    if os.name != "nt" or shutil.which("git"):
        assert shell
        assert Path(shell).exists()
    elif shell is not None:
        assert Path(shell).exists()


def test_scoreboard_does_not_count_a_shell_skip_as_a_failure():
    board = Scoreboard(mode="mock", profile="day")
    board.results = [
        {"name": "bugfix-test", "status": "pass"},
        {"name": "shell-script", "status": SHELL_SKIP_STATUS},
    ]
    assert board.ok
    summary = board.summary()
    assert summary["passed"] == 1
    assert summary["skipped"] == 1
    assert summary["failed"] == 0


def test_shell_checker_skips_when_no_posix_shell_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.shells.find_posix_shell", lambda: None)
    checker_path = repo_root() / "evals" / "shell-script" / "checker.py"
    spec = importlib.util.spec_from_file_location("shell_script_checker_skip", checker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    shutil.copytree(checker_path.parent / "starter", project)
    monkeypatch.setattr(sys, "argv", ["checker.py", str(project)])
    assert module.main() == SHELL_SKIP_CODE
