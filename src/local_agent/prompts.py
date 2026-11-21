"""Short, stable prompts. Local models follow a small instruction better than a long one."""

from __future__ import annotations

import os


def shell_name() -> str:
    return "PowerShell" if os.name == "nt" else "bash"


def system_prompt(cheat_sheet: str = "") -> str:
    shell = shell_name()
    base = (
        "You are a local coding agent. The repository is on disk. "
        "Accuracy matters more than speed.\n\n"
        f"The shell tool runs {shell}. Commands are killed when timeout_seconds "
        "elapses (default 120, maximum 600). Do not start a process you expect "
        "to keep running.\n\n"
        "Paths are relative to the project root. Do not leave the project.\n"
        "Use list_files to list a directory. It skips bin, obj, .git, build output, "
        "and ignored paths. Do not recurse those directories with the shell.\n"
        "Before editing a file, read it. old_string must match the file exactly once.\n"
        "After you change code, run the tests. Never say tests passed unless the "
        "command's exit code was 0.\n"
        "Do not mock the unit under test and do not replace the real entry point "
        "with a fake. Run the real command.\n"
        "Before you stop, re-check every acceptance criterion. Do not claim one "
        "is met unless you checked it against the code or a command you ran.\n"
        "Do not run git commit, git push, git reset --hard, or shutdown. "
        "A runner outside this chat commits and writes the report.\n"
        "If the task is ambiguous, do not guess. End your reply with an "
        "Open questions heading and list what is unresolved.\n"
        "When the task is done, stop calling tools and reply with a short summary."
    )
    sheet = cheat_sheet.strip()
    if not sheet:
        return base
    return (
        base
        + "\n\nCheat sheet (trust this section; do not create diagnostic projects, "
        "decompile, or probe with reflection to rediscover it):\n\n"
        + sheet
        + "\n"
    )


def plan_prompt(task: str) -> str:
    return (
        "Read the repository if you need to, then write a numbered plan for the task below. "
        "Do not edit files and do not run commands in this phase. "
        "A repo map of files and symbols is included when one was built. "
        "Use those paths, or list_files, instead of guessing directories. "
        "The plan should name the files you will change and how you will test them.\n\n"
        f"TASK:\n{task}"
    )


def implement_prompt(task: str, plan: str) -> str:
    return (
        "Implement the task. Follow the plan. Create and edit files with the tools. "
        "When a repo map is in the project context, use those paths instead of guessing. "
        "Do not mock the unit under test. Run the real entry point and the tests "
        "before you stop. Re-check every acceptance criterion, then stop calling tools "
        "and summarize what you changed.\n\n"
        f"PLAN:\n{plan}\n\nTASK:\n{task}"
    )


def fix_prompt(
    task: str,
    test_output: str,
    changed_files: str,
    *,
    smoke_failed: bool = False,
    verify_failed: bool = False,
    diff_text: str = "",
    traceback_files: str = "",
    false_pass_reason: str = "",
    cheat_sheet: str = "",
    api_reminder: bool = False,
) -> str:
    if false_pass_reason.strip():
        lead = (
            "The test command exited 0, but that is not success. "
            f"{false_pass_reason.strip()} "
            "Existing tests can pass when the new code was never written. "
            "Stop exploring. Write the files now and build early."
        )
    elif smoke_failed:
        lead = (
            "The smoke check failed after the tests passed. Fix the code so the real "
            "entry point runs. Do not mock the unit under test."
        )
    elif verify_failed:
        lead = (
            "A verify command failed after the tests passed. "
            "The acceptance command must exit 0. Fix the code so that command succeeds."
        )
    else:
        lead = "The tests failed. Fix the code."
    diff = diff_text.strip() or "(no diff)"
    cited = traceback_files.strip() or "(none)"
    reminder = ""
    sheet = cheat_sheet.strip()
    if api_reminder and sheet:
        reminder = (
            "\n\nThe build or compile errors look like API misuse. "
            "Follow this cheat sheet. Do not create a diagnostic project, "
            "decompile, or probe with reflection.\n\n"
            f"{sheet}\n"
        )
    return (
        f"{lead} Read the files you need, edit them, and run the tests again. "
        "Re-check every acceptance criterion before you stop. "
        "Do not repeat a tool call that just failed with the same arguments.\n\n"
        f"Changed files:\n{changed_files or '(none)'}\n\n"
        f"Diff (stat, then a capped patch):\n{diff}\n\n"
        f"Files mentioned in the traceback:\n{cited}\n\n"
        f"Test output:\n{test_output}\n\nTASK:\n{task}"
        f"{reminder}"
    )


def test_first_prompt(task: str, acceptance: str) -> str:
    return (
        "Write tests that encode the acceptance criteria. Do not implement the feature "
        "and do not edit production code. Create or edit test files only. "
        "The tests should fail until the feature exists. "
        "Do not mock the unit under test. Call the real functions or the real entry point. "
        "When the tests are written, stop calling tools and list the files.\n\n"
        f"ACCEPTANCE CRITERIA:\n{acceptance or '(none listed)'}\n\n"
        f"TASK:\n{task}"
    )


def test_first_repair_prompt(task: str, test_output: str) -> str:
    return (
        "The tests you wrote did not collect. The failure is in the tests "
        "(syntax or import of the test file), not a missing feature. "
        "Fix only the test files. Do not implement the feature and do not mock the unit under test.\n\n"
        f"Test output:\n{test_output}\n\nTASK:\n{task}"
    )


def review_prompt(
    task: str,
    diff: str,
    tests_ok: bool,
    *,
    acceptance: str = "",
    out_of_scope: str = "",
    context_block: str = "",
    test_first_files: str = "",
    evidence: str = "",
) -> str:
    status = "The test command passed." if tests_ok else "The test command did not pass."
    context = (context_block.rstrip() + "\n\n") if context_block.strip() else ""
    coverage = ""
    if test_first_files.strip():
        coverage = (
            "Tests vs criteria:\n"
            "say whether the locked test-first files exercise each acceptance criterion "
            "without mocking the unit under test. End the section with a line "
            "`Coverage: ok` or `Coverage: gap`.\n"
        )
    locked = ""
    if test_first_files.strip():
        locked = (
            "\n\nLOCKED TEST-FIRST FILES (these must still match the criteria and "
            f"must not have been weakened):\n{test_first_files.strip()}\n"
        )
    evidence_block = ""
    authority = ""
    if evidence.strip():
        authority = (
            "The harness checks below were executed by the harness and are authoritative. "
            "Do not request running, verifying, checking, or confirming anything those "
            "checks already cover. Do not claim a file, import, or symbol is missing when "
            "the changed-file contents show it.\n"
        )
        evidence_block = evidence.strip() + "\n\n"
    return (
        f"{context}"
        "Review the diff against the task and the project context. Do not call tools.\n"
        f"{authority}"
        "Reply with these sections and these exact headings:\n"
        "Acceptance criteria:\n"
        "one line per criterion, each ending in met or not met.\n"
        "Out-of-scope files touched:\n"
        "paths that this change edited outside the task, or none.\n"
        "Conflicts with AGENTS.md or decisions:\n"
        "each conflict, or none.\n"
        f"{coverage}"
        "Findings:\n"
        "one line per leftover item, or none. Start each item with [defect] or [verify-only]. "
        "[verify-only] means the item only asks to run, verify, check, or confirm. "
        "[defect] means wrong behaviour, a file or symbol that is not in the tree, "
        "or a criterion that is not met.\n"
        "Correctness: what the change does and whether it matches the task.\n"
        "Tests: what was verified and what was not.\n"
        "Risks: concrete leftover bugs or edge cases.\n"
        "Verdict: accept or fix. Use fix only when at least one finding is a [defect]. "
        "Do not vote fix for a [verify-only] item the harness checks already cover.\n"
        "Open questions: unresolved ambiguities, or none. Do not guess.\n\n"
        f"{status}\n\n"
        f"{evidence_block}"
        f"ACCEPTANCE CRITERIA FROM THE TASK:\n{acceptance or '(none listed)'}\n\n"
        f"OUT OF SCOPE FROM THE TASK:\n{out_of_scope or '(none listed)'}\n"
        f"{locked}\n"
        f"TASK:\n{task}\n\n"
        "DIFF (file list first, then as much of the patch as fits):\n"
        f"{diff}"
    )


def review_format_reminder() -> str:
    """Appended when a review reply is tool markup or is missing its sections."""

    return (
        "Your previous reply was not a usable review. This phase has no tools. "
        "Do not emit <tool_call> tags, </tool_call> tags, or any other tool markup. "
        "Write the review from the diff, the harness checks, and the project context "
        "already in this prompt. Those checks were executed by the harness and are "
        "authoritative. Do not ask to run, verify, check, or confirm them, and do not "
        "claim a file or symbol is missing when the prompt shows it. "
        "Use these exact headings, each on its own line:\n"
        "Acceptance criteria:\n"
        "Out-of-scope files touched:\n"
        "Conflicts with AGENTS.md or decisions:\n"
        "Verdict: accept or fix\n"
    )


def review_fix_prompt(task: str, review: str, changed_files: str) -> str:
    return (
        "The self-review rejected the change. Fix only the findings in the review. "
        "Do not rewrite files that already work, and do not replace a passing "
        "implementation with a new approach. Do not expand work the task marks out "
        "of scope. If a cheat sheet is in the system prompt, follow it and do not "
        "contradict it. If a finding is still ambiguous, list it under Open questions "
        "instead of guessing.\n\n"
        f"Review:\n{review}\n\n"
        f"Changed files:\n{changed_files or '(none)'}\n\n"
        f"TASK:\n{task}"
    )
