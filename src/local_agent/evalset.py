"""Small eval fixtures for the overnight harness.

`--mock` does not call llama-server. It checks that every fixture's hidden
checker fails on the starter tree and passes on the reference solution, and
that the task file validates. Live mode copies the starter into a throwaway
git repo, runs the agent, then runs the same checker.

The checker is not copied into the project, so the model does not see it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.overnight import load_task
from local_agent.paths import repo_root, runtime_dir
from local_agent.taskcheck import validate_task


@dataclass
class Fixture:
    name: str
    root: Path

    @property
    def task_path(self) -> Path:
        return self.root / "task.md"

    @property
    def starter(self) -> Path:
        return self.root / "starter"

    @property
    def solution(self) -> Path:
        return self.root / "solution"

    @property
    def checker(self) -> Path:
        return self.root / "checker.py"


@dataclass
class Scoreboard:
    mode: str
    profile: str
    results: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(item.get("status") == "pass" for item in self.results)

    def summary(self) -> dict[str, Any]:
        passed = sum(1 for item in self.results if item.get("status") == "pass")
        return {
            "mode": self.mode,
            "profile": self.profile,
            "passed": passed,
            "failed": len(self.results) - passed,
            "ok": self.ok,
            "scoreboard": str(self.path) if self.path else None,
            "results": self.results,
        }


def discover(root: Path | None = None) -> list[Fixture]:
    base = Path(root) if root is not None else repo_root() / "evals"
    if not base.is_dir():
        return []
    found: list[Fixture] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or path.name.startswith((".", "_")):
            continue
        if (path / "task.md").is_file() and (path / "checker.py").is_file() and (path / "starter").is_dir():
            found.append(Fixture(path.name, path))
    return found


def run_eval(
    *,
    profile_name: str = "quality",
    only: list[str] | None = None,
    mock: bool = False,
    base_url: str | None = None,
    max_minutes: float = 40.0,
    retries: int = 0,
    output_dir: Path | None = None,
    fixtures_root: Path | None = None,
) -> Scoreboard:
    selected = _select(discover(fixtures_root), only or [])
    mode = "mock" if mock else "live"
    board = Scoreboard(mode=mode, profile=profile_name)
    for fixture in selected:
        started = time.perf_counter()
        if mock:
            result = _mock_fixture(fixture)
        else:
            result = _live_fixture(
                fixture,
                profile_name=profile_name,
                base_url=base_url,
                max_minutes=max_minutes,
                retries=retries,
            )
        result["wall_seconds"] = round(time.perf_counter() - started, 3)
        board.results.append(result)
    board.path = _write_scoreboard(board, output_dir)
    return board


def _select(fixtures: list[Fixture], only: list[str]) -> list[Fixture]:
    if not fixtures:
        raise RuntimeError("no eval fixtures found under evals/")
    if not only:
        return fixtures
    by_name = {fixture.name: fixture for fixture in fixtures}
    missing = [name for name in only if name not in by_name]
    if missing:
        known = ", ".join(by_name)
        raise RuntimeError(f"unknown eval fixture(s): {', '.join(missing)}. Known: {known}")
    return [by_name[name] for name in only]


def _mock_fixture(fixture: Fixture) -> dict[str, Any]:
    """Prove the checker rejects the starter and accepts the solution."""

    base = Path(tempfile.mkdtemp(prefix=f"eval-{fixture.name}-"))
    try:
        starter = base / "starter"
        _copy_tree(fixture.starter, starter)
        task = load_task(fixture.task_path)
        problems = validate_task(task, starter)
        if problems:
            return _row(fixture.name, "fail", 0, "task does not validate: " + "; ".join(problems))
        starter_code, starter_out = _run_checker(fixture.checker, starter)
        if starter_code == 0:
            return _row(fixture.name, "fail", 0, "checker passed on the starter:\n" + starter_out)
        if not fixture.solution.is_dir():
            return _row(fixture.name, "fail", 0, "fixture has no solution/")
        solved = base / "solved"
        _copy_tree(fixture.starter, solved)
        _overlay(fixture.solution, solved)
        solved_code, solved_out = _run_checker(fixture.checker, solved)
        if solved_code != 0:
            return _row(fixture.name, "fail", 0, "checker failed on the solution:\n" + solved_out)
        return _row(fixture.name, "pass", 0, "starter fails, solution passes")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _live_fixture(
    fixture: Fixture,
    *,
    profile_name: str,
    base_url: str | None,
    max_minutes: float,
    retries: int,
) -> dict[str, Any]:
    from local_agent.config import get_profile
    from local_agent.overnight import make_client, run_with_retries

    base = Path(tempfile.mkdtemp(prefix=f"eval-live-{fixture.name}-"))
    project = base / "project"
    try:
        _copy_tree(fixture.starter, project)
        _git_init(project)
        profile = get_profile(profile_name)
        task = load_task(fixture.task_path)
        result = run_with_retries(
            retries=retries,
            task=task,
            project=project,
            profile=profile,
            client=make_client(profile, base_url),
            log_dir=base / "logs",
            max_iterations=4,
            max_seconds=max_minutes * 60,
            max_plan_turns=6,
            max_work_turns=16,
        )
        code, output = _run_checker(fixture.checker, project)
        agent_ok = bool(result.tests_passed and result.review_passed and result.smoke_passed)
        if agent_ok and code == 0:
            status = "pass"
            detail = "agent passed and checker passed"
        elif agent_ok and code != 0:
            status = "fail"
            detail = "agent passed its test command but the hidden checker failed:\n" + output
        else:
            status = "fail"
            detail = f"agent status {result.status}; checker exit {code}\n{output}"
        return _row(
            fixture.name,
            status,
            result.attempts,
            detail,
            extra={
                "agent_status": result.status,
                "branch": result.branch,
                "tests_passed": result.tests_passed,
                "review_passed": result.review_passed,
                "smoke_passed": result.smoke_passed,
            },
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _row(
    name: str,
    status: str,
    attempts: int,
    detail: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "status": status,
        "attempts": attempts,
        "detail": detail.strip(),
    }
    if extra:
        row.update(extra)
    return row


def _run_checker(checker: Path, project: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, str(checker), str(project)],
            cwd=checker.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        text = exc.stdout or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        return 124, text + "\nchecker timed out"
    return proc.returncode, proc.stdout or ""


def _copy_tree(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def _overlay(source: Path, dest: Path) -> None:
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _git_init(project: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    if not os.environ.get("GIT_AUTHOR_EMAIL"):
        subprocess.run(["git", "config", "user.email", "local-agent@localhost"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "Local Coding Agent"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.email=local-agent@localhost", "-c", "user.name=Local Coding Agent", "commit", "-m", "starter"],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _write_scoreboard(board: Scoreboard, output_dir: Path | None) -> Path:
    directory = Path(output_dir) if output_dir is not None else runtime_dir() / "evals"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = directory / f"scoreboard-{board.mode}-{stamp}.json"
    payload = board.summary()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# Eval scoreboard ({board.mode})",
        "",
        f"- Profile: {board.profile}",
        f"- Passed: {payload['passed']}",
        f"- Failed: {payload['failed']}",
        "",
        "| Fixture | Status | Attempts | Wall | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in board.results:
        detail = str(item.get("detail", "")).replace("\n", " ")[:160]
        wall = item.get("wall_seconds", "")
        lines.append(
            f"| {item.get('name', '')} | {item.get('status', '')} | {item.get('attempts', '')} | {wall} | {detail} |"
        )
    lines.append("")
    markdown = path.with_suffix(".md")
    markdown.write_text("\n".join(lines), encoding="utf-8")
    return path
