"""End-to-end smoke of the overnight loop.

`--mock` never opens a socket. A scripted client writes a bug, the runner's
own pytest sees it fail, and the next phase fixes it. That is the pipeline
the GPU cannot be asked to prove in CI.

`--live` points the same task at a llama-server you already started. It uses
a throwaway git repo so a confused model cannot touch a real project.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from local_agent.client import ChatResult, ToolCall
from local_agent.config import Profile, get_profile
from local_agent.overnight import OvernightResult, load_task, run_overnight
from local_agent.paths import repo_root

ADDER_BUG = "def add(a, b):\n    return a - b\n"
ADDER_TEST = "from adder import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
REVIEW = (
    "Correctness: add returns the sum of its arguments.\n"
    "Tests: test_add covers 2 + 3.\n"
    "Risks: non-numeric inputs are not handled.\n"
    "Verdict: accept\n"
)


class ScriptedSmokeClient:
    """Stand-in for llama-server. The profile is accepted and ignored."""

    def __init__(self, profile: Profile | None = None):
        self.profile = profile
        self.implement_calls = 0
        self.fix_calls = 0

    def chat(self, messages, *, tools, max_tokens):  # noqa: ANN001 - matches ChatClient.chat
        prompt = _first_user(messages)
        if "write a numbered plan" in prompt:
            return _text("1. Add adder.py with add(a, b).\n2. Add test_adder.py.\n3. Run pytest.")
        if prompt.startswith("Implement the task"):
            self.implement_calls += 1
            if self.implement_calls == 1:
                return _call("write", {"path": "adder.py", "content": ADDER_BUG})
            if self.implement_calls == 2:
                return _call("write", {"path": "test_adder.py", "content": ADDER_TEST})
            return _text("Implemented adder.py and test_adder.py.")
        if prompt.startswith("The tests failed"):
            self.fix_calls += 1
            if self.fix_calls == 1:
                return _call(
                    "edit",
                    {
                        "path": "adder.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                )
            return _text("Fixed add to return a sum.")
        if prompt.startswith("Review the diff"):
            return _text(REVIEW)
        return _text("Nothing to do.")


def run_mock_smoke(root: Path | None = None) -> OvernightResult:
    """Run the scripted overnight pipeline in a temporary git repository."""

    if root is None:
        base = "/tmp" if Path("/tmp").is_dir() else None
        tmp = Path(tempfile.mkdtemp(prefix="local-agent-smoke-", dir=base))
    else:
        tmp = Path(root)
        tmp.mkdir(parents=True, exist_ok=True)
    project = tmp / "project"
    project.mkdir(parents=True, exist_ok=True)
    _git_init(project)
    task = load_task(repo_root() / "tasks" / "smoke-task.md")
    profile = get_profile("day")
    # The harness reads these budgets off the profile. Keep the mock cheap.
    profile.agent_max_tokens = 256
    profile.agent_max_tokens_ceiling = 512
    profile.context = 8192
    log_dir = tmp / "logs"
    return run_overnight(
        task=task,
        project=project,
        profile=profile,
        client=ScriptedSmokeClient(profile),  # type: ignore[arg-type]
        log_dir=log_dir,
        max_iterations=3,
        max_seconds=120,
        max_plan_turns=2,
        max_work_turns=6,
        test_command=f"{sys.executable} -m pytest -q",
        lint_command=None,
    )


def run_live_smoke(profile_name: str, base_url: str | None, max_minutes: float) -> OvernightResult:
    """Run tasks/smoke-task.md against a real server, in a throwaway repo."""

    from local_agent.overnight import make_client

    base = "/tmp" if Path("/tmp").is_dir() else None
    tmp = Path(tempfile.mkdtemp(prefix="local-agent-live-", dir=base))
    project = tmp / "project"
    project.mkdir(parents=True, exist_ok=True)
    _git_init(project)
    profile = get_profile(profile_name)
    task = load_task(repo_root() / "tasks" / "smoke-task.md")
    client = make_client(profile, base_url)
    return run_overnight(
        task=task,
        project=project,
        profile=profile,
        client=client,
        log_dir=tmp / "logs",
        max_iterations=3,
        max_seconds=max_minutes * 60,
        max_plan_turns=6,
        max_work_turns=12,
        test_command=f"{sys.executable} -m pytest -q",
        lint_command=None,
    )


def _git_init(project: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    # Identity is supplied per commit when the repo has none. Setting it here
    # keeps `git status` quiet on machines that warn about a missing identity.
    if not os.environ.get("GIT_AUTHOR_EMAIL"):
        subprocess.run(
            ["git", "config", "user.email", "local-agent@localhost"],
            cwd=project,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Local Coding Agent"],
            cwd=project,
            check=True,
        )


def _first_user(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _text(content: str) -> ChatResult:
    return ChatResult(content=content, reasoning="", tool_calls=[], finish_reason="stop")


def _call(name: str, arguments: dict) -> ChatResult:
    return ChatResult(
        content="",
        reasoning="",
        tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )
