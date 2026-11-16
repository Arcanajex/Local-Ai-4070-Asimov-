"""Guards from two failed runs: leaked reviews, false passes, probes, cheat sheets."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from local_agent.client import ChatResult, ToolCall
from local_agent.config import get_profile
from local_agent.guardrails import (
    EXPLORATION_NUDGE,
    PhaseGuard,
    collect_cheat_sheets,
    false_pass_release,
    has_leaked_tool_markup,
    implementation_gap,
    missing_must_exist,
    planned_paths,
    strip_leaked_tool_markup,
    tool_block_reason,
)
from local_agent.harness import AgentHarness
from local_agent.logging_util import RunLogger
from local_agent.overnight import load_task, parse_task, run_overnight, working_tree_diff
from local_agent.paths import repo_root
from local_agent.prompts import review_fix_prompt, review_format_reminder
from local_agent.taskcheck import review_form_problems, validate_task
from local_agent.tools import Workspace


GOOD_REVIEW = (
    "Acceptance criteria:\n- it works: met\n"
    "Out-of-scope files touched:\nnone\n"
    "Conflicts with AGENTS.md or decisions:\nnone\n"
    "Verdict: accept\n"
)


def test_leaked_tool_markup_is_stripped_and_a_real_review_is_not():
    leaked = (repo_root() / "evals" / "leaked-review" / "leaked_review.txt").read_text(encoding="utf-8")
    assert has_leaked_tool_markup(leaked)
    assert review_form_problems(leaked)
    stripped = strip_leaked_tool_markup(leaked)
    assert "<tool_call" not in stripped.lower()
    assert "Let me read the existing files first." in stripped
    assert not has_leaked_tool_markup(GOOD_REVIEW)
    assert review_form_problems(GOOD_REVIEW) == []
    assert strip_leaked_tool_markup(GOOD_REVIEW) == GOOD_REVIEW


def test_malformed_review_is_reprompted_and_never_starts_review_fix(tmp_path):
    project = _repo(tmp_path / "repo")
    leaked = (repo_root() / "evals" / "leaked-review" / "leaked_review.txt").read_text(encoding="utf-8")
    seen: list[str] = []

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            seen.append(prompt)
            if "Implement the task." in prompt:
                return _call("write", {"path": "marker.txt", "content": "ok\n"})
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(leaked)
            return _text("1. Create marker.txt")

    result = run_overnight(
        task=_task(tmp_path, "---\ntest_command: true\n---\n" + _body("marker")),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    assert result.status == "review_malformed"
    assert result.review_passed is False
    assert result.tests_passed is True
    assert not any("review-fix" == _phase(line) for line in _events(tmp_path))
    assert any("review_reprompt" in line for line in _events(tmp_path))
    assert "was not sent to review-fix" in result.review
    assert review_format_reminder() in "\n".join(seen)


def test_a_reprompted_review_can_still_reject_for_real(tmp_path):
    project = _repo(tmp_path / "repo")
    leaked = (repo_root() / "evals" / "leaked-review" / "leaked_review.txt").read_text(encoding="utf-8")
    state = {"reviews": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Implement the task." in prompt:
                return _call("write", {"path": "marker.txt", "content": "ok\n"})
            if "The self-review rejected" in prompt:
                return _text("left the working file alone")
            if "Review the diff" in prompt or "not a usable review" in prompt:
                state["reviews"] += 1
                if state["reviews"] == 1:
                    return _text(leaked)
                return _text(GOOD_REVIEW.replace("met", "not met", 1).replace("accept", "fix"))
            return _text("1. Create marker.txt")

    result = run_overnight(
        task=_task(tmp_path, "---\ntest_command: true\n---\n" + _body("marker")),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    phases = [_phase(line) for line in _events(tmp_path)]
    assert "review-fix" in phases
    assert result.tests_passed is True
    # The second reply was a real rejection, so review-fix ran. It did not
    # rewrite the file, and the follow-up review was never reached because the
    # scripted client keeps saying "not met" — the run must not pass.
    assert result.status != "passed"
    assert (project / "marker.txt").read_text(encoding="utf-8") == "ok\n"


def test_empty_diff_and_turn_budget_do_not_count_as_success(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "test_keep.py").write_text("def test_keep():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "test_keep.py"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "tests"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    plan = "1. Create feature.py with answer()."
    assert planned_paths(plan) == ["feature.py"]
    assert implementation_gap(plan, "(no changes relative to HEAD)\n")
    assert implementation_gap(
        plan,
        "diff --git a/feature.py b/feature.py\n+++ b/feature.py\n+def answer():\n+    return 42\n",
    ) is None
    assert implementation_gap("1. Read the notes.", "diff --git a/notes.md b/notes.md\n+++ b/notes.md\n+x\n") is None

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            if "not success" in prompt or "False pass" in prompt:
                return _call("write", {"path": "feature.py", "content": "def answer():\n    return 42\n"})
            if "Implement the task." in prompt:
                return _text("still reading")
            return _text(plan)

    result = run_overnight(
        task=_task(
            tmp_path,
            "---\n"
            f"test_command: {sys.executable} -m pytest -q\n"
            "---\n" + _body("feature"),
        ),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    assert result.status == "passed", result.review
    assert (project / "feature.py").is_file()
    assert any("false_pass" in line for line in _events(tmp_path))

    project2 = _repo(tmp_path / "stuck")
    (project2 / "test_keep.py").write_text("def test_keep():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "test_keep.py"], cwd=project2, check=True)
    subprocess.run(["git", "commit", "-m", "tests"], cwd=project2, check=True, stdout=subprocess.DEVNULL)

    class Stuck:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            if "Implement the task." in prompt:
                return _call("read", {"path": "test_keep.py"})
            return _text(plan)

    stuck = run_overnight(
        task=_task(tmp_path, "---\ntest_command: true\n---\n" + _body("stuck")),
        project=project2,
        profile=_day(),
        client=Stuck(),
        log_dir=tmp_path / "stuck-logs",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=1,
    )
    assert stuck.status == "false_pass"
    assert stuck.tests_passed is False


def test_turn_budget_after_real_files_goes_to_review_without_looping(tmp_path):
    """The guard used to stay shut after implement hit the turn budget.

    A real run then spent every later fix round on a tree that already passed
    must_exist, tests, and smoke. The model said the files were in place.
    Committing during implement made `git diff HEAD` empty, so the planned-file
    check never saw the cumulative work.
    """

    project = _repo(tmp_path / "repo")
    (project / "test_keep.py").write_text("def test_keep():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "test_keep.py"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "tests"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    plan = "1. Create feature.py with answer()."
    fixes = {"n": 0}

    class Client:
        def __init__(self):
            self.impl = 0

        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            if "not success" in prompt or "False pass" in prompt:
                fixes["n"] += 1
                return _text("The task is complete. 46/46. All files are already in place and fully verified.")
            if "Implement the task." in prompt:
                self.impl += 1
                if self.impl == 1:
                    return _call(
                        "write",
                        {"path": "feature.py", "content": "def answer():\n    return 42\n"},
                    )
                if self.impl == 2:
                    return _call(
                        "shell",
                        {"command": "git add feature.py && git commit -m 'add feature'"},
                    )
                return _call("read", {"path": "feature.py"})
            return _text(plan)

    result = run_overnight(
        task=_task(
            tmp_path,
            "---\n"
            "test_command: true\n"
            "must_exist:\n"
            "  - feature.py\n"
            "---\n" + _body("feature"),
        ),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=8,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=3,
    )
    phases = [_phase(line) for line in _events(tmp_path)]
    assert result.status == "passed", result.review
    assert result.tests_passed is True
    assert result.review_passed is True
    assert result.iterations == 1
    assert fixes["n"] == 0
    assert "review" in phases
    assert not any(phase.startswith("fix-") for phase in phases)
    assert (project / "feature.py").is_file()
    events = [json.loads(line) for line in _events(tmp_path)]
    guards = [item for item in events if item.get("kind") == "false_pass_guard"]
    assert guards and guards[-1]["action"] == "release"
    assert "cumulative diff" in guards[-1]["summary"]
    assert "turn_budget" in guards[-1]["summary"]
    must = [item for item in events if item.get("kind") == "must_exist"]
    assert must and must[-1]["missing"] == [] and must[-1]["passed"] is True
    # HEAD is the agent's commit, so a diff against HEAD is empty. The base is not.
    head_diff = working_tree_diff(project)
    assert "feature.py" not in head_diff or "no changes" in head_diff
    base = next(item["commit"] for item in events if item.get("kind") == "task_base")
    cumulative = working_tree_diff(project, base)
    assert "feature.py" in cumulative
    assert implementation_gap(plan, cumulative) is None
    assert implementation_gap(plan, head_diff)


def test_uncommitted_turn_budget_does_not_need_a_noop_fix(tmp_path):
    project = _repo(tmp_path / "repo")
    plan = "1. Create feature.py with answer()."
    fixes = {"n": 0}

    class Client:
        def __init__(self):
            self.impl = 0

        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            if "not success" in prompt or "False pass" in prompt:
                fixes["n"] += 1
                return _text("All files are already in place and fully verified.")
            if "Implement the task." in prompt:
                self.impl += 1
                if self.impl == 1:
                    return _call("write", {"path": "feature.py", "content": "def answer():\n    return 42\n"})
                return _call("read", {"path": "feature.py"})
            return _text(plan)

    result = run_overnight(
        task=_task(tmp_path, "---\ntest_command: true\nmust_exist:\n  - feature.py\n---\n" + _body("plain")),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=8,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    phases = [_phase(line) for line in _events(tmp_path)]
    assert result.status == "passed", result.review
    assert result.iterations == 1
    assert fixes["n"] == 0
    assert not any(phase.startswith("fix-") for phase in phases)
    assert "review" in phases
    release = [json.loads(line) for line in _events(tmp_path) if "\"false_pass_guard\"" in line]
    assert release and release[-1]["action"] == "release"


def test_two_noop_fix_rounds_release_the_guard_and_review(tmp_path):
    project = _repo(tmp_path / "repo")
    plan = "1. Create feature.py with answer()."
    assert false_pass_release("implement produced an empty diff", consecutive_noop_fixes=1) is None
    assert false_pass_release("implement produced an empty diff", consecutive_noop_fixes=2)
    assert false_pass_release(None, implement_status="turn_budget")
    assert false_pass_release("diff is empty relative to the files the plan named (feature.py)", fix_produced_changes=True)

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            if "Implement the task." in prompt:
                return _call("read", {"path": "README.md"})
            if "not success" in prompt or "False pass" in prompt:
                return _text("The task is complete. All files are already in place and fully verified.")
            return _text(plan)

    (project / "README.md").write_text("notes\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "notes"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    result = run_overnight(
        task=_task(tmp_path, "---\ntest_command: true\n---\n" + _body("stuck-loop")),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=8,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=1,
    )
    phases = [phase for phase in (_phase(line) for line in _events(tmp_path)) if phase]
    fix_phases = [phase for phase in phases if phase.startswith("fix-")]
    assert fix_phases == ["fix-1", "fix-2"]
    assert "review" in phases
    assert result.status == "passed", result.review
    assert result.tests_passed is True
    events = [json.loads(line) for line in _events(tmp_path)]
    guards = [item for item in events if item.get("kind") == "false_pass_guard"]
    assert [item["action"] for item in guards] == ["block", "block", "release"]
    assert "two consecutive fix rounds" in guards[-1]["summary"]
    assert "no file changes" in guards[-1]["summary"]
    assert not (project / "feature.py").exists()


def test_must_exist_blocks_a_green_test_command(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    assert missing_must_exist(loose, ["feature.py", "test_answer"], "passed") == ["feature.py", "test_answer"]
    (loose / "feature.py").write_text("x\n", encoding="utf-8")
    assert missing_must_exist(loose, ["feature.py", "test_answer"], "test_answer PASSED") == []
    project = _repo(tmp_path / "repo")

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "must_exist check failed" in prompt:
                return _call("write", {"path": "feature.py", "content": "def answer():\n    return 1\n"})
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            return _text("1. Create feature.py")

    result = run_overnight(
        task=_task(
            tmp_path,
            "---\ntest_command: true\nmust_exist:\n  - feature.py\n---\n" + _body("gate"),
        ),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    assert result.status == "passed"
    assert "must_exist" in "\n".join(_events(tmp_path))
    assert (project / "feature.py").is_file()


def test_exploration_cap_nudges_then_refuses_and_blocks_probes(tmp_path):
    reads = [_call("read", {"path": "note.txt", "offset": index + 1}) for index in range(8)]
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    client = _Script(reads)
    profile = _day()
    harness = AgentHarness(client, Workspace(tmp_path), RunLogger(tmp_path / "logs"), profile)
    result = harness.run(
        "build it",
        max_turns=8,
        guard=PhaseGuard(exploration_cap=2, exploration_bonus_turns=0),
    )
    assert result.status == "exploration_capped"
    blob = "\n".join(message.get("content") or "" for call in client.calls for message in call["messages"])
    assert EXPLORATION_NUDGE in blob
    assert "Refusing further exploration" in blob
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "exploration_nudge" in log
    assert "exploration_escalated" in log

    blocked = tool_block_reason(
        "shell",
        {"command": "ilspycmd Silk.NET.dll"},
        root=tmp_path,
        plan_paths=(),
    )
    assert blocked and "decompil" in blocked.lower()
    assert tool_block_reason("shell", {"command": "dotnet new classlib -o ApiCheck"}, root=tmp_path, plan_paths=())
    assert tool_block_reason("write", {"path": "diag/Probe.cs", "content": "class P {}"}, root=tmp_path, plan_paths=())
    assert tool_block_reason("shell", {"command": "mkdir diag"}, root=tmp_path, plan_paths=())
    assert (
        tool_block_reason(
            "write",
            {"path": "src/App.csproj", "content": "<Project></Project>\n"},
            root=tmp_path,
            plan_paths=("src/App.csproj",),
        )
        is None
    )
    (tmp_path / "App.csproj").write_text("<Project></Project>\n", encoding="utf-8")
    assert (
        tool_block_reason(
            "edit",
            {"path": "App.csproj", "old_string": "a", "new_string": "b"},
            root=tmp_path,
            plan_paths=(),
        )
        is None
    )
    guard = PhaseGuard(plan_paths=("src/App.csproj",))
    client2 = _Script([
        _call("shell", {"command": "ilspycmd x.dll", "timeout_seconds": 5}),
        _text("stopped"),
    ])
    ran = AgentHarness(client2, Workspace(tmp_path), RunLogger(tmp_path / "logs2"), profile).run(
        "probe",
        max_turns=3,
        guard=guard,
    )
    assert ran.status == "completed"
    assert "command blocked" in client2.calls[1]["messages"][-1]["content"]


def test_exploration_bonus_turns_are_only_for_a_read_only_phase(tmp_path):
    (tmp_path / "note.txt").write_text("x\n", encoding="utf-8")
    client = _Script([_call("read", {"path": "note.txt", "offset": index + 1}) for index in range(6)])
    result = AgentHarness(client, Workspace(tmp_path), RunLogger(tmp_path / "logs"), _day()).run(
        "look",
        max_turns=2,
        guard=PhaseGuard(exploration_cap=100, exploration_bonus_turns=2),
    )
    assert result.status == "turn_budget"
    assert len(client.calls) == 4
    assert "exploration_bonus" in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")


def test_cheat_sheet_is_in_the_system_prompt_and_repeated_on_api_errors(tmp_path):
    project = _repo(tmp_path / "repo")
    sheet = "## Silk.NET cheat sheet\n\n" + ("GL.Clear(mask)\n" * 40) + "CHEAT_TOKEN_END\n"
    (project / "AGENTS.md").write_text("# Rules\n\nKeep it small.\n\n" + sheet, encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "agents"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    found = collect_cheat_sheets(project, [], ["cheat sheet"])
    assert "CHEAT_TOKEN_END" in found
    assert found.strip().startswith("## Silk.NET cheat sheet")
    systems: list[str] = []
    users: list[str] = []
    task = _task(
        tmp_path,
        "---\n"
        f"test_command: {sys.executable} -c \"import sys; print('error CS1061'); sys.exit(1)\"\n"
        "---\n" + _body("sheet"),
    )
    # The implement write is skipped: the test command fails, so the fix
    # prompt is where the API-misuse reminder has to show up.
    class Failing:
        def chat(self, messages, *, tools, max_tokens):
            systems.append(messages[0]["content"])
            users.append(_user(messages))
            prompt = _user(messages)
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            return _text("1. Create marker.txt")

    result = run_overnight(
        task=task,
        project=project,
        profile=_day(),
        client=Failing(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=1,
        context_char_limit=180,
    )
    assert result.status != "passed"
    implement_or_fix_systems = [text for text, user in zip(systems, users) if "Implement the task." in user or "API misuse" in user or "tests failed" in user.lower() or "The tests failed" in user]
    assert any("CHEAT_TOKEN_END" in text for text in systems)
    assert any("CHEAT_TOKEN_END" in text and "API misuse" in text for text in users)
    # The short context cap cuts the user-facing project block. The sheet still
    # arrives whole on the system prompt.
    assert any("CHEAT_TOKEN_END" not in user and "Project context" in user for user in users)
    assert any("CHEAT_TOKEN_END" in text for text in implement_or_fix_systems)


def test_review_fix_revert_keeps_the_passing_snapshot(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "check.py").write_text(
        "import pathlib, sys\n"
        "ok = pathlib.Path('good.txt').is_file() and not pathlib.Path('bad.txt').exists()\n"
        "sys.exit(0 if ok else 1)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "check.py"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "check"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    command = f"{sys.executable} check.py"
    wrote = {"fix": 0, "impl": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Fix only the findings" in prompt or "The self-review rejected" in prompt:
                wrote["fix"] += 1
                if wrote["fix"] == 1:
                    return _call("write", {"path": "bad.txt", "content": "broken\n"})
                return _text("stopped")
            if "Implement the task." in prompt:
                wrote["impl"] += 1
                if wrote["impl"] == 1:
                    return _call("write", {"path": "good.txt", "content": "ok\n"})
                return _text("wrote good.txt")
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW.replace(": met", ": not met").replace("accept", "fix"))
            return _text("1. Create good.txt")

    result = run_overnight(
        task=_task(tmp_path, f"---\ntest_command: {command}\n---\n" + _body("revert")),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=3,
    )
    assert result.status == "review_fix_reverted"
    assert result.review_passed is False
    assert result.tests_passed is True
    assert (project / "good.txt").is_file()
    assert not (project / "bad.txt").exists()
    assert result.snapshot_refs
    assert result.restored_snapshot == result.snapshot_refs[-1]
    show = subprocess.check_output(["git", "show-ref", result.snapshot_refs[0]], cwd=project, text=True)
    assert result.snapshot_refs[0] in show
    listed = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", result.snapshot_refs[0]],
        cwd=project,
        text=True,
    )
    assert "good.txt" in listed
    assert "bad.txt" not in listed
    log = "\n".join(_events(tmp_path))
    assert "\"kind\": \"snapshot\"" in log
    assert "snapshot_restore" in log
    prompt = review_fix_prompt("task", "review", "good.txt")
    assert "Fix only the findings" in prompt
    assert "Do not rewrite files that already work" in prompt


def test_front_matter_turn_budgets_bom_and_unparsed_front_matter(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "package.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "package.json"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "pkg"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    body = _body("budget")
    text = "---\nplan_turns: 3\nimplement_turns: 4\nexploration_cap: 2\ntest_command: true\n---\n" + body
    task_path = tmp_path / "task.md"
    task_path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    loaded = load_task(task_path)
    assert loaded.front_matter_error is None
    assert loaded.plan_turns == 3
    assert loaded.implement_turns == 4
    assert loaded.test_command == "true"
    assert validate_task(loaded, project) == []

    wrote = {"n": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            if "Implement the task." in prompt:
                wrote["n"] += 1
                if wrote["n"] == 1:
                    return _call("write", {"path": "marker.txt", "content": "ok\n"})
                return _text("wrote marker.txt")
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(GOOD_REVIEW)
            return _text("1. Create marker.txt")

    result = run_overnight(
        task=loaded,
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=1,
        max_seconds=60,
    )
    assert result.status == "passed"
    phases = [json.loads(line) for line in _events(tmp_path) if "\"phase_start\"" in line]
    budgets = {item["phase"]: item["max_turns"] for item in phases}
    assert budgets["plan"] == 3
    assert budgets["implement"] == 4

    broken = tmp_path / "broken.md"
    broken.write_text("---\ntest_command: npm test\n" + _body("broken"), encoding="utf-8")
    bad = load_task(broken)
    problems = validate_task(bad, project)
    assert any("front matter could not be parsed" in item for item in problems)
    assert not any("test_command is required" in item for item in problems)
    assert parse_task("\ufeff---\ntest_command: npm test\n---\n" + _body("bom")).test_command == "npm test"


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


def _phase(line: str) -> str:
    if "\"phase_start\"" not in line and "\"kind\": \"phase_start\"" not in line:
        return ""
    try:
        return str(json.loads(line).get("phase") or "")
    except json.JSONDecodeError:
        return ""


class _Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, tools, max_tokens):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        if not self.replies:
            return _text("stopped")
        return self.replies.pop(0)
