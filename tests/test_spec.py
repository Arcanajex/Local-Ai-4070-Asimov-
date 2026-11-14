"""Project context, spec review, task template, report location, and main-branch guard."""

from __future__ import annotations

import subprocess
from pathlib import Path

from local_agent.cli import main
from local_agent.config import get_profile
from local_agent.context import compact_messages, estimate_tokens, prompt_budget
from local_agent.overnight import OvernightResult, fit_review_diff, load_task, parse_task, run_overnight
from local_agent.project_context import (
    DEFAULT_CONTEXT_CHARS,
    build_project_context,
    context_fits_budget,
    with_project_context,
)
from local_agent.prompts import system_prompt
from local_agent.queue import run_queue
from local_agent.taskcheck import review_problems, validate_task


def test_context_block_caps_files_and_lists_every_path(tmp_path):
    (tmp_path / "AGENTS.md").write_text("AGENT_RULE\n" + ("a" * 20_000), encoding="utf-8")
    (tmp_path / "notes.md").write_text("DECISION\n" + ("b" * 20_000), encoding="utf-8")
    block = build_project_context(tmp_path, ["notes.md", "../outside.md"], limit=4_000)
    assert len(block) <= 4_000
    assert "AGENTS.md" in block
    assert "notes.md" in block
    assert "[truncated - read AGENTS.md for the rest]" in block
    assert "[truncated - read notes.md for the rest]" in block
    assert "outside.md" in block
    assert "escapes the project" in block


def test_default_context_cap_fits_the_quality_retry_budget():
    profile = get_profile("quality")
    block = "Project context\n" + ("x" * (DEFAULT_CONTEXT_CHARS - 20))
    assert len(block) <= DEFAULT_CONTEXT_CHARS
    assert profile.context == 40960
    assert profile.agent_max_tokens == 16384
    assert profile.agent_max_tokens_ceiling == 24576
    assert context_fits_budget(
        block,
        context_tokens=profile.context,
        generation_tokens=profile.agent_max_tokens,
    )
    assert context_fits_budget(
        block,
        context_tokens=profile.context,
        generation_tokens=profile.agent_max_tokens_ceiling,
    )
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": with_project_context("TASK: add a function\n", block)},
    ]
    assert estimate_tokens(messages) <= prompt_budget(profile.context, profile.agent_max_tokens_ceiling)


def test_compaction_keeps_the_first_user_message_context():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": with_project_context("TASK build it", "Project context\nUNIQUE_CONTEXT_MARKER")},
    ]
    for index in range(12):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "shell", "arguments": {"command": str(index)}}}],
            }
        )
        messages.append({"role": "tool", "content": "output " + ("x" * 4000)})
    compacted = compact_messages(messages, 900)
    joined = "\n".join(str(message.get("content") or "") for message in compacted)
    assert "UNIQUE_CONTEXT_MARKER" in joined
    assert compacted[0]["role"] == "system"


def test_fit_review_diff_keeps_the_file_list_and_names_omissions():
    stat = "# Changes since HEAD\n\n a.py | 1 +\n b.py | 1 +\n c.py | 1 +\n\n# Patch\n"
    patch = "".join(
        f"diff --git a/{name} b/{name}\n+++ b/{name}\n+line from {name}\n" for name in ("a.py", "b.py", "c.py")
    )
    text = stat + patch
    fitted = fit_review_diff(text, limit=len(stat) + 80)
    assert "a.py | 1 +" in fitted
    assert "b.py | 1 +" in fitted
    assert "c.py | 1 +" in fitted
    assert "Files omitted from the patch" in fitted
    assert "- b.py" in fitted
    assert "- c.py" in fitted
    assert "line from c.py" not in fitted


def test_review_not_met_or_conflict_fails_and_met_does_not():
    good = (
        "Acceptance criteria:\n- returns 5: met\n"
        "Out-of-scope files touched:\nnone\n"
        "Conflicts with AGENTS.md or decisions:\nnone\n"
        "Verdict: accept\n"
    )
    assert review_problems(good) == []
    missed = good.replace("met", "not met", 1)
    assert any("not met" in item for item in review_problems(missed))
    conflict = good.replace(
        "Conflicts with AGENTS.md or decisions:\nnone",
        "Conflicts with AGENTS.md or decisions:\nAGENTS.md forbids new dependencies",
    )
    assert any(item.startswith("conflict:") for item in review_problems(conflict))


def test_non_python_project_requires_test_command(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    task = parse_task("# Title\n\n## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] y\n\n## Out of scope\n\n- z\n")
    problems = validate_task(task, tmp_path)
    assert any("test_command" in item for item in problems)
    explicit = parse_task(
        "---\ntest_command: npm test\n---\n# Title\n\n## Goal\n\nX\n\n## Acceptance criteria\n\n- [ ] y\n\n## Out of scope\n\n- z\n"
    )
    assert validate_task(explicit, tmp_path) == []


def test_validate_command_and_queue_refuses_before_the_first_task(tmp_path, capsys):
    project = _repo(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "01-ok.md").write_text(_valid("one"), encoding="utf-8")
    (task_dir / "02-bad.md").write_text("# Missing sections\n\nJust do it.\n", encoding="utf-8")
    code = main(["validate", str(task_dir), "--project", str(project)])
    assert code == 1
    assert "02-bad.md" in capsys.readouterr().err
    calls: list[str] = []
    try:
        run_queue(
            task_dir=task_dir,
            project=project,
            profile=get_profile("day"),
            client=None,
            log_dir=tmp_path / "logs",
            archive=False,
            run_task=_noop(calls),
        )
    except RuntimeError as exc:
        assert "02-bad.md" in str(exc)
        assert "--no-strict" in str(exc)
    else:
        raise AssertionError("queue should have refused")
    assert calls == []
    run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        strict=False,
        continue_on_failure=True,
        run_task=_noop(calls),
    )
    assert calls == ["01-ok.md", "02-bad.md"]


def test_queue_requires_allow_main_on_the_default_branch(tmp_path):
    project = _repo(tmp_path / "repo", branch="main")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "01-ok.md").write_text(_valid("one"), encoding="utf-8")
    try:
        run_queue(
            task_dir=task_dir,
            project=project,
            profile=get_profile("day"),
            client=None,
            log_dir=tmp_path / "logs",
            archive=False,
            run_task=_noop([]),
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("main should be refused")
    assert "--allow-main" in message
    assert "-AllowMain" in message
    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        allow_main=True,
        archive=False,
        run_task=_passing(),
    )
    assert result.status == "passed"


def test_context_reaches_every_phase_and_reports_stay_uncommitted(tmp_path):
    project = _repo(tmp_path / "repo")
    (project / "AGENTS.md").write_text("RULE_KEEP_THE_MARKER\n", encoding="utf-8")
    (project / "notes.md").write_text("DECISION_USE_STDLIB\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md", "notes.md"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Spec Test", "-c", "user.email=spec@example.com", "commit", "-m", "context"],
        cwd=project,
        check=True,
    )
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "---\n"
        "test_command: true\n"
        "context:\n"
        "  - notes.md\n"
        "---\n"
        + _valid("adder"),
        encoding="utf-8",
    )
    seen: list[str] = []

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            from local_agent.client import ChatResult

            prompt = next(message["content"] for message in messages if message["role"] == "user")
            seen.append(prompt)
            if "Review the diff" in prompt:
                return ChatResult(
                    content=(
                        "Acceptance criteria:\n- marker: met\n"
                        "Out-of-scope files touched:\nnone\n"
                        "Conflicts with AGENTS.md or decisions:\nnone\n"
                        "Verdict: accept\n"
                    ),
                    reasoning="",
                    tool_calls=[],
                    finish_reason="stop",
                )
            return ChatResult(content="Plan and implement with no open questions.", reasoning="", tool_calls=[], finish_reason="stop")

    result = run_overnight(
        task=load_task(task_path),
        project=project,
        profile=get_profile("day"),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=2,
        max_work_turns=2,
    )
    assert result.status == "passed"
    assert len(seen) >= 3
    assert all("RULE_KEEP_THE_MARKER" in prompt for prompt in seen)
    assert all("DECISION_USE_STDLIB" in prompt for prompt in seen)
    assert all(prompt.startswith("Project context") for prompt in seen)
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "project context" in log
    report = result.report_path.read_text(encoding="utf-8")
    assert "## Open questions" in report
    assert result.report_path.parent == project / ".agent"
    names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=project, text=True
    )
    assert ".agent/" not in names
    assert "AGENT_REPORT.md" not in names


def test_not_met_review_triggers_one_fix_then_can_pass(tmp_path):
    project = _repo(tmp_path / "repo")
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "---\ntest_command: true\n---\n" + _valid("fix me"),
        encoding="utf-8",
    )
    reviews = {"n": 0}

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            from local_agent.client import ChatResult

            prompt = next(message["content"] for message in messages if message["role"] == "user")
            if "The self-review rejected" in prompt:
                return ChatResult(content="Fixed the criterion.", reasoning="", tool_calls=[], finish_reason="stop")
            if "Review the diff" in prompt:
                reviews["n"] += 1
                if reviews["n"] == 1:
                    body = (
                        "Acceptance criteria:\n- marker: not met\n"
                        "Out-of-scope files touched:\nnone\n"
                        "Conflicts with AGENTS.md or decisions:\nnone\n"
                        "Verdict: fix\n"
                    )
                else:
                    body = (
                        "Acceptance criteria:\n- marker: met\n"
                        "Out-of-scope files touched:\nnone\n"
                        "Conflicts with AGENTS.md or decisions:\nnone\n"
                        "Verdict: accept\n"
                    )
                return ChatResult(content=body, reasoning="", tool_calls=[], finish_reason="stop")
            return ChatResult(content="done", reasoning="", tool_calls=[], finish_reason="stop")

    result = run_overnight(
        task=load_task(task_path),
        project=project,
        profile=get_profile("day"),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=2,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )
    assert reviews["n"] == 2
    assert result.status == "passed"
    assert result.review_passed


def _valid(name: str) -> str:
    return (
        f"# {name}\n\n## Goal\n\nShip {name}.\n\n"
        "## Acceptance criteria\n\n- [ ] it works\n\n"
        "## Out of scope\n\n- no extra files\n"
    )


def _repo(path: Path, branch: str = "integration") -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-b", branch], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-c", "user.name=Spec Test", "-c", "user.email=spec@example.com", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
    )
    return path


def _noop(calls: list[str]):
    def run_task(*, task, project, profile, client, log_dir, max_seconds, max_iterations, init_repo, **_extra):
        calls.append(task.path.name)
        return OvernightResult(
            status="tests_failed",
            branch="",
            report_path=None,
            commit=None,
            iterations=1,
            tests_passed=False,
            log_dir=log_dir,
            review_passed=False,
        )

    return run_task


def _passing():
    def run_task(*, task, project, profile, client, log_dir, max_seconds, max_iterations, init_repo, **_extra):
        branch = "agent/ok"
        subprocess.run(["git", "checkout", "-b", branch], cwd=project, check=True)
        (project / "ok.txt").write_text("ok\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Spec Test", "-c", "user.email=spec@example.com", "commit", "-m", "ok"],
            cwd=project,
            check=True,
        )
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=project, text=True).strip()
        return OvernightResult(
            status="passed",
            branch=branch,
            report_path=None,
            commit=sha,
            iterations=1,
            tests_passed=True,
            log_dir=log_dir,
        )

    return run_task
