"""Short, stable prompts. Local models follow a small instruction better than a long one."""

from __future__ import annotations

import os


def shell_name() -> str:
    return "PowerShell" if os.name == "nt" else "bash"


def system_prompt() -> str:
    shell = shell_name()
    return (
        "You are a local coding agent. The repository is on disk. "
        "Accuracy matters more than speed.\n\n"
        f"The shell tool runs {shell}. Commands are killed when timeout_seconds "
        "elapses (default 120, maximum 600). Do not start a process you expect "
        "to keep running.\n\n"
        "Paths are relative to the project root. Do not leave the project.\n"
        "Before editing a file, read it. old_string must match the file exactly once.\n"
        "After you change code, run the tests. Never say tests passed unless the "
        "command's exit code was 0.\n"
        "Do not run git commit, git push, git reset --hard, or shutdown. "
        "A runner outside this chat commits and writes the report.\n"
        "If the task is ambiguous, do not guess. End your reply with an "
        "Open questions heading and list what is unresolved.\n"
        "When the task is done, stop calling tools and reply with a short summary."
    )


def plan_prompt(task: str) -> str:
    return (
        "Read the repository if you need to, then write a numbered plan for the task below. "
        "Do not edit files and do not run commands in this phase. "
        "The plan should name the files you will change and how you will test them.\n\n"
        f"TASK:\n{task}"
    )


def implement_prompt(task: str, plan: str) -> str:
    return (
        "Implement the task. Follow the plan. Create and edit files with the tools. "
        "Run the tests before you stop. When the work is in place, stop calling tools "
        "and summarize what you changed.\n\n"
        f"PLAN:\n{plan}\n\nTASK:\n{task}"
    )


def fix_prompt(task: str, test_output: str, changed_files: str) -> str:
    return (
        "The tests failed. Fix the code. Read the files you need, edit them, and run "
        "the tests again. Do not repeat a tool call that just failed with the same arguments.\n\n"
        f"Changed files:\n{changed_files or '(none)'}\n\n"
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
) -> str:
    status = "The test command passed." if tests_ok else "The test command did not pass."
    context = (context_block.rstrip() + "\n\n") if context_block.strip() else ""
    return (
        f"{context}"
        "Review the diff against the task and the project context. Do not call tools.\n"
        "Reply with these sections and these exact headings:\n"
        "Acceptance criteria:\n"
        "one line per criterion, each ending in met or not met.\n"
        "Out-of-scope files touched:\n"
        "paths that this change edited outside the task, or none.\n"
        "Conflicts with AGENTS.md or decisions:\n"
        "each conflict, or none.\n"
        "Correctness: what the change does and whether it matches the task.\n"
        "Tests: what was verified and what was not.\n"
        "Risks: concrete leftover bugs or edge cases.\n"
        "Verdict: accept or fix. Use fix only for a defect you can point at in the diff.\n"
        "Open questions: unresolved ambiguities, or none. Do not guess.\n\n"
        f"{status}\n\n"
        f"ACCEPTANCE CRITERIA FROM THE TASK:\n{acceptance or '(none listed)'}\n\n"
        f"OUT OF SCOPE FROM THE TASK:\n{out_of_scope or '(none listed)'}\n\n"
        f"TASK:\n{task}\n\n"
        "DIFF (file list first, then as much of the patch as fits):\n"
        f"{diff}"
    )


def review_fix_prompt(task: str, review: str, changed_files: str) -> str:
    return (
        "The self-review rejected the change. Fix the code so every acceptance "
        "criterion is met and every conflict is resolved. Do not expand work "
        "the task marks out of scope. If something is still ambiguous, list it "
        "under Open questions instead of guessing.\n\n"
        f"Review:\n{review}\n\n"
        f"Changed files:\n{changed_files or '(none)'}\n\n"
        f"TASK:\n{task}"
    )
