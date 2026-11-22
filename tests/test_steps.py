"""Step parsing, per-step budgets, rollback, and one replan."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from local_agent.client import ChatResult, ToolCall
from local_agent.config import get_profile, write_override
from local_agent.logging_util import RunLogger
from local_agent.overnight import (
    _run_phase,
    _run_step_gate,
    apply_project_defaults,
    load_task,
    parse_task,
    run_overnight,
)
from local_agent.shells import command_fails, command_succeeds, python_shell
from local_agent.steps import (
    Step,
    commands_match,
    normalize_verify_command,
    parse_steps,
    resolve_step_settings,
)
from local_agent.tools import PENDING_FILE_NOTE, Workspace


GOOD_REVIEW = (
    "Acceptance criteria:\n- it works: met\n"
    "Out-of-scope files touched:\nnone\n"
    "Conflicts with AGENTS.md or decisions:\nnone\n"
    "Verdict: accept\n"
)

_TWO_STEPS = """### Step 1
Goal: Write good.txt
Files: good.txt
Verify: {ok}

### Step 2
Goal: Write bad.txt
Files: bad.txt
Verify: {bad}
"""

_BAD_AGAIN = """### Step 1
Goal: Write bad.txt again
Files: bad.txt
Verify: {bad}
"""


def test_parse_steps_accepts_shapes_and_drops_a_plain_plan():
    heading = parse_steps(
        "### Step 1\nGoal: Add the loader\nFiles: src/Mesh.cs, tests/MeshTests.cs\nVerify: dotnet build\n"
        "### Step 2\nGoal: Wire the editor\nFiles: none\nVerify: dotnet test --filter Name~A|B\n"
    )
    assert [step.goal for step in heading] == ["Add the loader", "Wire the editor"]
    assert heading[0].files == ["src/Mesh.cs", "tests/MeshTests.cs"]
    assert heading[1].files == []
    assert heading[1].verify == "dotnet test --filter Name~A|B"
    assert heading[0].index == 1 and heading[1].index == 2

    numbered = parse_steps(
        "1. Goal: Add the loader\n   Files: src\\EditorApp.cs\n   Verify: dotnet build\n"
    )
    assert numbered[0].files == ["src/EditorApp.cs"]
    assert numbered[0].verify == "dotnet build"

    inline = parse_steps("1. Goal: Add the loader | Files: src/Mesh.cs | Verify: dotnet build\n")
    assert inline[0].goal == "Add the loader"
    assert inline[0].files == ["src/Mesh.cs"]
    assert inline[0].verify == "dotnet build"

    powershell = "& 'C:\\Program Files\\python.exe' -m pytest -q"
    quoted = parse_steps(f"### Step 1\nGoal: Run the suite\nFiles: test_adder.py\nVerify: {powershell}\n")
    assert len(quoted) == 1
    assert quoted[0].verify == powershell

    assert parse_steps("1. Create marker.txt\n2. Run pytest.\n") == []
    assert parse_steps("### Step 1\nGoal: what this step makes true\nFiles: a.cs\nVerify: the one command for this step\n") == []

    many = "\n".join(
        f"### Step {index}\nGoal: Do {index}\nFiles: f{index}.cs\nVerify: exit 0\n" for index in range(1, 9)
    )
    capped = parse_steps(many, max_steps=6)
    assert [step.index for step in capped] == [1, 2, 3, 4, 5, 6]
    assert capped[-1].goal == "Do 6"
    restarted = parse_steps(many, max_steps=2, index_start=3)
    assert [step.index for step in restarted] == [3, 4]
    assert commands_match("  exit   0 ", "exit 0")
    assert not commands_match("exit 0", "exit 1")


def test_parse_steps_strips_markdown_quotes_and_joins_command_lines():
    wrapped = parse_steps(
        "### Step 1\nGoal: Run the suite\nFiles: none\nVerify: `dotnet test`\n"
    )
    assert wrapped[0].verify == "dotnet test"

    quoted = parse_steps(
        '### Step 1\nGoal: Run the suite\nFiles: none\nVerify: "dotnet test"\n'
    )
    assert quoted[0].verify == "dotnet test"
    single = parse_steps(
        "### Step 1\nGoal: Build\nFiles: none\nVerify: 'dotnet build'\n"
    )
    assert single[0].verify == "dotnet build"

    fenced = parse_steps(
        "### Step 1\nGoal: Run the suite\nFiles: a.cs\n"
        "Verify: ```powershell\n"
        "dotnet test\n"
        "```\n"
    )
    assert fenced[0].verify == "dotnet test"
    assert fenced[0].files == ["a.cs"]

    two_lines = parse_steps(
        "### Step 2\nGoal: Build then test\nFiles: a.cs\n"
        "Verify: ```\n"
        "dotnet build\n"
        "dotnet test\n"
        "```\n"
        "\n"
        "### Step 3\nGoal: Ship\nFiles: b.cs\nVerify: dotnet test\n"
    )
    assert [step.verify for step in two_lines] == ["dotnet build && dotnet test", "dotnet test"]
    assert two_lines[1].goal == "Ship"

    assert normalize_verify_command('  `"dotnet test"`  ') == "dotnet test"
    assert normalize_verify_command("dotnet build\ndotnet test") == "dotnet build && dotnet test"
    assert normalize_verify_command("dotnet test \\\n--filter Name~A") == "dotnet test --filter Name~A"
    assert normalize_verify_command("dotnet test --filter Name~A|B") == "dotnet test --filter Name~A|B"
    powershell = "& 'C:\\Program Files\\python.exe' -m pytest -q"
    assert normalize_verify_command(powershell) == powershell
    assert normalize_verify_command(f"`{powershell}`") == powershell

    assert commands_match("`dotnet test`", "dotnet test")
    assert commands_match("```powershell\ndotnet test\n```", '"dotnet test"')
    assert commands_match("  `exit   0` ", "exit 0")
    assert not commands_match("dotnet test", "dotnet build")
    assert not commands_match("", "dotnet test")
    assert not commands_match("   ", "   ")

    echoed = parse_steps(
        "### Step 1\nGoal: what this step makes true\nFiles: a.cs\n"
        "Verify: `the one command for this step`\n"
    )
    assert echoed == []


def test_malformed_verify_falls_back_and_a_real_failure_does_not(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    task = parse_task(f"---\ntest_command: {command_succeeds()}\n---\n# t\n")
    task.verify_timeout = 30
    logger = RunLogger(tmp_path / "logs")
    try:
        missing = parse_steps(
            "### Step 1\nGoal: Check\nFiles: none\nVerify: `not-a-real-command-zz-local-agent`\n"
        )[0]
        assert missing.verify == "not-a-real-command-zz-local-agent"
        ok, output = _run_step_gate(
            project, task, missing, task.test_command, None, False, logger
        )
        assert ok, output
        text = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
        assert "step_verify_invalid" in text
        assert "passed=True" in text or "passed=true" in text

        genuine = parse_steps(
            f"### Step 1\nGoal: Check\nFiles: none\nVerify: {command_fails()}\n"
        )[0]
        ok, output = _run_step_gate(
            project, task, genuine, task.test_command, None, False, logger
        )
        assert not ok, output
        text = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
        invalids = [line for line in text.splitlines() if "step_verify_invalid" in line]
        assert len(invalids) == 1

        chained_fail = parse_steps(
            "### Step 1\nGoal: Check\nFiles: none\n"
            f"Verify: {command_succeeds()} && {command_fails()}\n"
        )[0]
        # The task command succeeds. Falling back would hide this real failure.
        ok, output = _run_step_gate(
            project, task, chained_fail, task.test_command, None, False, logger
        )
        assert not ok, output
        assert "$ " + command_fails() in output

        task_fails = parse_task(f"---\ntest_command: {command_fails()}\n---\n# t\n")
        task_fails.verify_timeout = 30
        recovered = parse_steps(
            "### Step 1\nGoal: Check\nFiles: none\n"
            f"Verify: {command_fails()} || {command_succeeds()}\n"
        )[0]
        ok, output = _run_step_gate(
            project, task_fails, recovered, task_fails.test_command, None, False, logger
        )
        assert ok, output
        text = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
        assert len([line for line in text.splitlines() if "step_verify_invalid" in line]) == 1

        # Backticks are stripped before the shell sees the command. The task
        # command fails, so a fallback would not pass.
        echoed = Step(index=1, goal="Check", files=[], verify="`echo hi-from-verify`")
        ok, output = _run_step_gate(
            project, task_fails, echoed, task_fails.test_command, None, False, logger
        )
        assert ok, output
        assert "hi-from-verify" in output
        text = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
        assert len([line for line in text.splitlines() if "step_verify_invalid" in line]) == 1
    finally:
        logger.close()


def test_step_plan_read_of_a_missing_file_is_pending(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "keep.txt").write_text("here\n", encoding="utf-8")
    logger = RunLogger(tmp_path / "logs")

    class Client:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, *, tools, max_tokens):
            self.turn += 1
            if self.turn == 1:
                return _call("read", {"path": "BrandNew.cs"})
            if self.turn == 2:
                return _call("read", {"path": "keep.txt"})
            return _text("### Step 1\nGoal: Add it\nFiles: BrandNew.cs\nVerify: exit 0\n")

    try:
        result = _run_phase(
            Client(),
            Workspace(project),
            logger,
            _day(),
            "Split the plan into small verified steps.",
            allowed={"read", "list_files"},
            max_turns=4,
            deadline=time.monotonic() + 30,
            phase="step-plan",
        )
        assert result.status == "completed"
        events = [
            json.loads(line)
            for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        reads = [item for item in events if item.get("kind") == "tool_result" and item.get("name") == "read"]
        assert len(reads) == 2
        assert reads[0]["ok"] is True
        assert f"BrandNew.cs: {PENDING_FILE_NOTE}" in reads[0]["output"]
        assert "file not found" not in reads[0]["output"]
        assert reads[1]["ok"] is True
        assert "here" in reads[1]["output"]

        class Implement:
            def __init__(self):
                self.done = False

            def chat(self, messages, *, tools, max_tokens):
                if not self.done:
                    self.done = True
                    return _call("read", {"path": "BrandNew.cs"})
                return _text("done")

        later = _run_phase(
            Implement(),
            Workspace(project),
            logger,
            _day(),
            "Implement this step only.",
            allowed={"read", "list_files"},
            max_turns=3,
            deadline=time.monotonic() + 30,
            phase="step-1",
        )
        assert later.status == "completed"
        events = [
            json.loads(line)
            for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        implement_reads = [
            item
            for item in events
            if item.get("kind") == "tool_result"
            and item.get("name") == "read"
            and "BrandNew.cs" in str(item.get("output"))
            and item.get("ok") is False
        ]
        assert implement_reads
        assert "file not found" in implement_reads[-1]["output"]
    finally:
        logger.close()


def test_step_settings_follow_task_then_agents_then_profile(tmp_path):
    profile = get_profile("day")
    assert profile.steps is True
    assert profile.max_steps == 6
    assert profile.step_turns == 12
    assert profile.step_replans == 1
    bare = parse_task("# Title\n\n## Goal\n\nX\n\n## Acceptance criteria\n\n- y\n\n## Out of scope\n\n- z\n")
    defaults = resolve_step_settings(
        bare.steps,
        bare.max_steps,
        bare.step_turns,
        bare.step_replans,
        profile_enabled=profile.steps,
        profile_max_steps=profile.max_steps,
        profile_step_turns=profile.step_turns,
        profile_replans=profile.step_replans,
    )
    assert defaults.enabled is True
    assert defaults.max_steps == 6
    assert defaults.step_turns == 12
    assert defaults.replans == 1

    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        "---\nsteps: false\nmax_steps: 4\nstep_turns: 9\nstep_replans: 0\n---\n# Agents\n",
        encoding="utf-8",
    )
    apply_project_defaults(bare, project)
    from_agents = resolve_step_settings(
        bare.steps,
        bare.max_steps,
        bare.step_turns,
        bare.step_replans,
        profile_enabled=profile.steps,
        profile_max_steps=profile.max_steps,
        profile_step_turns=profile.step_turns,
        profile_replans=profile.step_replans,
    )
    assert from_agents.enabled is False
    assert from_agents.max_steps == 4
    assert from_agents.step_turns == 9
    assert from_agents.replans == 0

    task = parse_task(
        "---\nsteps: true\nstep_turns: 3\nmax_steps: 100\nstep_replans: -1\n---\n"
        "# Title\n\n## Goal\n\nX\n\n## Acceptance criteria\n\n- y\n\n## Out of scope\n\n- z\n"
    )
    apply_project_defaults(task, project)
    overridden = resolve_step_settings(
        task.steps,
        task.max_steps,
        task.step_turns,
        task.step_replans,
        profile_enabled=profile.steps,
        profile_max_steps=profile.max_steps,
        profile_step_turns=profile.step_turns,
        profile_replans=profile.step_replans,
    )
    assert overridden.enabled is True
    assert overridden.step_turns == 3
    assert overridden.max_steps == 8
    assert overridden.replans == 0

    path = tmp_path / "overrides.json"
    write_override("day", {"steps": False, "step_turns": 7, "max_steps": 2, "step_replans": 3}, path)
    loaded = get_profile("day", override_path=path)
    assert loaded.steps is False
    assert loaded.step_turns == 7
    assert loaded.max_steps == 2
    assert loaded.step_replans == 3


def test_steps_false_keeps_one_implement_phase(tmp_path):
    project = _repo(tmp_path / "repo")

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Implement the task." in prompt:
                return _call("write", {"path": "marker.txt", "content": "ok\n"})
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            return _text("1. Create marker.txt")

    result = run_overnight(
        task=_task(tmp_path, f"---\nsteps: false\ntest_command: {command_succeeds()}\n---\n" + _body("off")),
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
    phases = [_phase(line) for line in _events(tmp_path)]
    assert "implement" in phases
    assert "step-plan" not in phases
    assert "step_start" not in (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")


def test_each_step_gets_its_own_turn_budget_and_a_fresh_prompt(tmp_path):
    project = _repo(tmp_path / "repo")
    fresh: list[int] = []

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Split the plan into small verified steps" in prompt:
                return _text(
                    "### Step 1\nGoal: Touch nothing\nFiles: none\nVerify: " + command_fails() + "\n"
                )
            if "Implement this step only" in prompt or "Fix this step only" in prompt:
                fresh.append(len(messages))
                return _call("read", {"path": "missing.txt"})
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            return _text("1. Create marker.txt")

    result = run_overnight(
        task=_task(
            tmp_path,
            "---\nsteps: true\nstep_turns: 2\nstep_replans: 0\nmax_steps: 1\n"
            f"test_command: {command_succeeds()}\n---\n" + _body("budget"),
        ),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=30,
    )
    assert result.status == "step_failed"
    assert result.tests_passed is False
    phases = [json.loads(line) for line in _events(tmp_path) if '"phase_start"' in line]
    budgets = {item["phase"]: item["max_turns"] for item in phases}
    assert "implement" not in budgets
    assert budgets["step-1"] == 2
    assert budgets["step-1-fix-1"] == 2
    assert budgets["step-1-retry"] == 2
    # Each implement and fix phase starts as system + user only. A later turn
    # in that same phase is longer, so a count of 2 is a fresh conversation.
    assert fresh.count(2) >= 6
    text_log = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
    assert "step_start 1/" in text_log
    assert "step_fail 1/" in text_log
    assert "\n" + _replan_lines(tmp_path) == "\n"
    assert not any(item.get("kind") == "replan" for item in _json_events(tmp_path))


def test_a_failed_step_rolls_back_and_replans_once(tmp_path):
    project = _repo(tmp_path / "repo")
    # Double quotes inside the Python source so PowerShell 5.1 can single-quote
    # the whole argument. A single quote in that string would have to be doubled.
    need_bad = python_shell(
        "-c",
        'import pathlib, sys; sys.exit(0 if pathlib.Path("bad.txt").is_file() else 1)',
    )
    prompts: list[str] = []
    fresh: list[int] = []

    class Client:
        def __init__(self):
            self.wrote: set[str] = set()

        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Split the plan into small verified steps" in prompt:
                return _text(_TWO_STEPS.format(ok=command_succeeds(), bad=command_fails()))
            if "Re-plan the remaining steps" in prompt:
                return _text(_BAD_AGAIN.format(bad=command_fails()))
            if "Implement this step only" in prompt:
                prompts.append(prompt)
                fresh.append(len(messages))
                current = prompt.split("## Current step", 1)[-1]
                if "Files: good.txt" in current and "good" not in self.wrote:
                    self.wrote.add("good")
                    return _call("write", {"path": "good.txt", "content": "ok\n"})
                if "Files: bad.txt" in current and "bad" not in self.wrote:
                    self.wrote.add("bad")
                    return _call("write", {"path": "bad.txt", "content": "no\n"})
                return _text("step done")
            if "Fix this step only" in prompt or "The tests failed" in prompt or "verify command failed" in prompt:
                fresh.append(len(messages))
                return _text("cannot fix this step")
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            return _text("1. Create good.txt")

    result = run_overnight(
        task=_task(
            tmp_path,
            "---\nsteps: true\nstep_replans: 1\nmax_steps: 3\nstep_turns: 4\n"
            f"test_command: {need_bad}\n---\n" + _body("rollback"),
        ),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=6,
    )
    assert result.status == "step_failed", result.review
    assert result.tests_passed is False
    assert (project / "good.txt").read_text(encoding="utf-8") == "ok\n"
    assert not (project / "bad.txt").exists()
    assert any("[done]" in prompt and "[current]" in prompt for prompt in prompts)
    assert 2 in fresh
    text_log = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
    assert "step_start " in text_log
    assert "step_pass " in text_log
    assert "step_fail " in text_log
    assert "replan " in text_log
    replans = [item for item in _json_events(tmp_path) if item.get("kind") == "replan"]
    assert len(replans) == 1
    assert replans[0]["index"] == 2
    assert replans[0]["replans"] == 1
    phases = [_phase(line) for line in _events(tmp_path)]
    assert "step-replan" in phases
    assert "step-replan-2" not in phases
    assert "review" in phases
    report = result.report_path.read_text(encoding="utf-8")
    assert "Status: step_failed" in report
    assert "stopped on step 2 after 1 replan" in report


def _replan_lines(tmp_path: Path) -> str:
    lines = []
    for line in (tmp_path / "logs" / "events.log").read_text(encoding="utf-8").splitlines():
        if " replan " in f" {line} ":
            lines.append(line)
    return "\n".join(lines)


def _task(tmp_path: Path, text: str):
    path = tmp_path / "task.md"
    path.write_text(text, encoding="utf-8")
    return load_task(path)


def _body(name: str) -> str:
    return (
        f"# {name}\n\n## Goal\n\nShip {name}.\n\n"
        "## Acceptance criteria\n\n- [ ] it works\n\n"
        "## Out of scope\n\n- no extra files\n"
    )


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


def _user(messages) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _events(tmp_path: Path) -> list[str]:
    return (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()


def _json_events(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in _events(tmp_path)]


def _phase(line: str) -> str:
    if '"phase_start"' not in line:
        return ""
    try:
        return str(json.loads(line).get("phase") or "")
    except json.JSONDecodeError:
        return ""
