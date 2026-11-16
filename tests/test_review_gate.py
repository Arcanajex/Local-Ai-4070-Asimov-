"""The reviewer sees harness results, and a verify-only fix vote can pass."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from local_agent.client import ChatResult, ToolCall
from local_agent.config import get_profile, write_override
from local_agent.overnight import (
    apply_project_defaults,
    parse_task,
    review_verify_only_enabled,
    run_overnight,
)
from local_agent.review_gate import (
    build_harness_evidence,
    contradicted_by_tree,
    decide_review,
    parse_test_counts,
)

MARKER = "using Xunit;\nclass ObjLoader {}\n"

VERIFY_REVIEW = (
    "Acceptance criteria:\n- it works: met\n"
    "Out-of-scope files touched:\nnone\n"
    "Conflicts with AGENTS.md or decisions:\nnone\n"
    "Tests:\nHarness tests passed.\nNeed to run --frames 5.\n"
    "Risks:\n- need to verify ObjLoader exists\n"
    "Open questions:\n- please confirm the build was run\n"
    "Verdict: fix\n"
)

MIXED_REVIEW = (
    "Acceptance criteria:\n- it works: met\n"
    "Out-of-scope files touched:\nnone\n"
    "Conflicts with AGENTS.md or decisions:\nnone\n"
    "Findings:\n"
    "- [verify-only] need to run --frames 5\n"
    "- [defect] answer() returns 4 instead of 5\n"
    "Verdict: fix\n"
)

MISSING_REVIEW = (
    "Acceptance criteria:\n- it works: met\n"
    "Out-of-scope files touched:\nnone\n"
    "Conflicts with AGENTS.md or decisions:\nnone\n"
    "Risks:\n- `using Xunit;` is missing from the test file.\n"
    "Verdict: fix\n"
)


def test_parse_test_counts_from_pytest_dotnet_cargo_and_xunit():
    assert parse_test_counts("============================== 46 passed in 1.2s ==============================") == "46 passed"
    assert parse_test_counts("=========== 45 passed, 1 failed, 2 skipped in 2.1s ===========") == (
        "45 passed, 1 failed, 2 skipped"
    )
    dotnet = "Passed!  - Failed:     0, Passed:    46, Skipped:     0, Total:    46, Duration: 1 s"
    assert parse_test_counts(dotnet) == "46 passed, 0 failed, 0 skipped, 46 total"
    cargo = "test result: ok. 46 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"
    assert parse_test_counts(cargo) == "46 passed, 0 failed, 0 skipped"
    xunit = "Total tests: 46. Passed: 46. Failed: 0. Skipped: 0."
    assert parse_test_counts(xunit) == "46 passed, 0 failed, 0 skipped, 46 total"
    assert parse_test_counts("exit_code: 0\n--- output ---\n") is None


def test_evidence_lists_commands_counts_must_exist_smoke_and_file_text(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "marker.txt").write_text(MARKER, encoding="utf-8")
    (project / "gone.py").write_text("x = 1\n", encoding="utf-8")
    output = "exit_code: 0\n--- output ---\n46 passed in 0.01s\nObjLoader\n\nSmoke check passed.\nwindow ok\n"
    block = build_harness_evidence(
        project,
        test_command="dotnet test",
        lint_command=None,
        tests_passed=True,
        test_output=output,
        must_exist=["marker.txt", "ObjLoader"],
        smoke_passed=True,
        smoke_command="python app.py --frames 5",
        changed_files=["marker.txt"],
    )
    assert "executed by the harness" in block
    assert "authoritative" in block
    assert "Build/test command: dotnet test" in block
    assert "passed (46 passed)" in block
    assert "marker.txt: present" in block
    assert "ObjLoader: present in test output" in block
    assert "Smoke: passed" in block
    assert "python app.py --frames 5" in block
    assert "window ok" in block
    assert "using Xunit;" in block
    assert "class ObjLoader" in block
    assert "Do not request running, verifying" in block


def test_decide_review_verify_only_mixed_and_contradicted(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "marker.txt").write_text(MARKER, encoding="utf-8")
    verify = decide_review(VERIFY_REVIEW, project, checks_green=True, allow_verify_only_pass=True)
    assert verify.action == "pass"
    assert any("--frames 5" in item for item in verify.verify_items)
    assert any("ObjLoader" in item for item in verify.verify_items)
    assert verify.defects == []

    red = decide_review(VERIFY_REVIEW, project, checks_green=False, allow_verify_only_pass=True)
    assert red.action == "fix"
    assert red.problems == ["verdict: fix"]

    disabled = decide_review(VERIFY_REVIEW, project, checks_green=True, allow_verify_only_pass=False)
    assert disabled.action == "fix"

    mixed = decide_review(MIXED_REVIEW, project, checks_green=True, allow_verify_only_pass=True)
    assert mixed.action == "fix"
    assert any("returns 4" in item for item in mixed.defects)

    missing = decide_review(MISSING_REVIEW, project, checks_green=True, allow_verify_only_pass=True)
    assert missing.action == "pass"
    assert missing.contradicted
    assert contradicted_by_tree("`using Xunit;` is missing from the test file.", project)
    assert any("Xunit" in item for item in missing.contradicted)

    # feature.py is not on disk, so the claim stands and the vote stays fix.
    assert not (project / "feature.py").exists()
    stayed = decide_review(
        _wrapped("Risks:\n- feature.py is missing\n"),
        project,
        checks_green=True,
        allow_verify_only_pass=True,
    )
    assert stayed.action == "fix"
    assert not stayed.contradicted

    not_met = decide_review(
        _wrapped("Acceptance criteria:\n- need to verify the window: not met\n"),
        project,
        checks_green=True,
        allow_verify_only_pass=True,
    )
    assert not_met.action == "fix"


def test_review_verify_only_pass_resolution(tmp_path):
    unset = parse_task("---\ntest_command: true\n---\n# t\n")
    profile = get_profile("day")
    assert profile.review_verify_only_pass is True
    assert review_verify_only_enabled(unset, profile) is True

    forced = parse_task("---\nreview_verify_only_pass: false\n---\n# t\n")
    assert review_verify_only_enabled(forced, profile) is False
    profile.review_verify_only_pass = False
    assert review_verify_only_enabled(unset, profile) is False
    again = parse_task("---\nreview_verify_only_pass: true\n---\n# t\n")
    assert review_verify_only_enabled(again, profile) is True

    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.md").write_text("---\nreview_verify_only_pass: false\n---\n# Agents\n", encoding="utf-8")
    task = parse_task("---\ntest_command: true\n---\n# t\n")
    apply_project_defaults(task, project)
    assert task.review_verify_only_pass is False

    write_override("day", {"review_verify_only_pass": False})
    assert get_profile("day").review_verify_only_pass is False


def test_verify_only_fix_with_green_checks_passes(tmp_path):
    project = _repo(tmp_path / "repo")
    seen: list[str] = []
    count = f"{sys.executable} -c \"print('46 passed in 0.01s')\" #"
    result = _run(
        tmp_path,
        project,
        "---\n"
        f"test_command: {count}\n"
        "smoke_command: true\n"
        "must_exist:\n"
        "  - marker.txt\n"
        "---\n" + _body("marker"),
        VERIFY_REVIEW,
        seen,
    )
    assert result.status == "passed", result.review
    assert result.review_passed is True
    assert result.tests_passed is True
    assert result.smoke_passed is True
    phases = [_phase(line) for line in _events(tmp_path)]
    assert "review-fix" not in phases
    prompt = _review_prompt(seen)
    assert "authoritative" in prompt
    assert "46 passed" in prompt
    assert "marker.txt: present" in prompt
    assert "Smoke: passed" in prompt
    assert "using Xunit;" in prompt
    assert "ObjLoader" in prompt
    assert "Files changed since the task base:" in prompt
    assert "Do not request running, verifying, checking, or confirming" in prompt
    event = _kind(tmp_path, "review_verify_only_pass")
    blob = " ".join(event["items"])
    assert "--frames 5" in blob
    assert "ObjLoader" in blob
    assert "confirm the build" in blob


def test_verify_only_fix_with_a_failing_check_still_fixes(tmp_path):
    project = _repo(tmp_path / "repo")
    result = _run(
        tmp_path,
        project,
        "---\ntest_command: false\n---\n" + _body("marker"),
        VERIFY_REVIEW,
        [],
        max_iterations=1,
    )
    phases = [_phase(line) for line in _events(tmp_path)]
    assert "review-fix" in phases
    assert result.review_passed is False
    assert result.status != "passed"
    assert _kinds(tmp_path).count("review_verify_only_pass") == 0


def test_mixed_defect_and_verify_still_fixes(tmp_path):
    project = _repo(tmp_path / "repo")
    result = _run(
        tmp_path,
        project,
        "---\ntest_command: true\n---\n" + _body("marker"),
        MIXED_REVIEW,
        [],
    )
    phases = [_phase(line) for line in _events(tmp_path)]
    assert "review-fix" in phases
    assert result.status != "passed"
    assert result.review_passed is False
    assert _kinds(tmp_path).count("review_verify_only_pass") == 0


def test_false_missing_claim_contradicted_by_the_tree_is_dropped(tmp_path):
    project = _repo(tmp_path / "repo")
    result = _run(
        tmp_path,
        project,
        "---\ntest_command: true\n---\n" + _body("marker"),
        MISSING_REVIEW,
        [],
    )
    assert result.status == "passed", result.review
    assert result.review_passed is True
    assert "review-fix" not in [_phase(line) for line in _events(tmp_path)]
    event = _kind(tmp_path, "review_finding_contradicted")
    assert any("Xunit" in item for item in event["items"])
    assert "using Xunit;" in (project / "marker.txt").read_text(encoding="utf-8")
    assert _kinds(tmp_path).count("review_verify_only_pass") == 0


def test_task_can_disable_verify_only_pass(tmp_path):
    project = _repo(tmp_path / "repo")
    result = _run(
        tmp_path,
        project,
        "---\ntest_command: true\nreview_verify_only_pass: false\n---\n" + _body("marker"),
        VERIFY_REVIEW,
        [],
    )
    assert "review-fix" in [_phase(line) for line in _events(tmp_path)]
    assert result.review_passed is False
    assert result.status != "passed"
    assert _kinds(tmp_path).count("review_verify_only_pass") == 0


def _wrapped(middle: str) -> str:
    return (
        "Acceptance criteria:\n- it works: met\n"
        "Out-of-scope files touched:\nnone\n"
        "Conflicts with AGENTS.md or decisions:\nnone\n"
        f"{middle}\n"
        "Verdict: fix\n"
    )


def _run(tmp_path, project, task_text, review, seen, *, max_iterations=2):
    class Client:
        def chat(self, messages, *, tools, max_tokens):
            prompt = _user(messages)
            seen.append(prompt)
            if "Implement the task." in prompt:
                return _call("write", {"path": "marker.txt", "content": MARKER})
            if "The self-review rejected" in prompt or "The tests failed" in prompt:
                return _text("left the file")
            if "Review the diff" in prompt or "not a usable review" in prompt:
                return _text(review)
            return _text("1. Create marker.txt")

    return run_overnight(
        task=_task(tmp_path, task_text),
        project=project,
        profile=_day(),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=max_iterations,
        max_seconds=60,
        max_plan_turns=1,
        max_work_turns=2,
    )


def _task(tmp_path: Path, text: str):
    path = tmp_path / "task.md"
    path.write_text(text, encoding="utf-8")
    return parse_task(path.read_text(encoding="utf-8"), path)


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
    if '"phase_start"' not in line:
        return ""
    try:
        return str(json.loads(line).get("phase") or "")
    except json.JSONDecodeError:
        return ""


def _kinds(tmp_path: Path) -> list[str]:
    kinds = []
    for line in _events(tmp_path):
        try:
            kinds.append(str(json.loads(line).get("kind") or ""))
        except json.JSONDecodeError:
            continue
    return kinds


def _kind(tmp_path: Path, kind: str) -> dict:
    for line in _events(tmp_path):
        event = json.loads(line)
        if event.get("kind") == kind:
            return event
    raise AssertionError(f"{kind} was not logged")


def _review_prompt(seen: list[str]) -> str:
    for prompt in seen:
        if "Review the diff" in prompt:
            return prompt
    raise AssertionError("review prompt was not sent")
