"""Process-tree kills, memory pauses, and the queue watchdog.

The shell tests are bounded by a thread join so a regression that blocks on
an open pipe fails the test instead of hanging the job. They run on Linux
and on Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from local_agent.client import ChatResult
from local_agent.config import get_profile
from local_agent.memory_guard import (
    MemorySample,
    MemorySettings,
    ensure_headroom,
    read_memory,
    resolve_memory_settings,
    resolve_watchdog_minutes,
)
from local_agent.overnight import OvernightResult, apply_project_defaults, parse_task, run_overnight
from local_agent.procman import child_env, configure_dotnet, install_error_mode
from local_agent.queue import load_state, run_queue
from local_agent.shells import python_shell
from local_agent.smoke_gate import run_smoke
from local_agent.tools import run_command

_HOLD = """\
import os
import subprocess
import sys
import time
from pathlib import Path

pidfile = Path(sys.argv[1])
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pidfile.write_text(f"{os.getppid()}\\n{os.getpid()}\\n{grand.pid}\\n", encoding="utf-8")
sys.stdout.write("seen-partial\\n")
sys.stdout.flush()
time.sleep(120)
"""


def test_grandchild_holding_the_pipe_returns_and_the_tree_dies(tmp_path):
    script = tmp_path / "hold.py"
    pidfile = tmp_path / "pids.txt"
    script.write_text(_HOLD, encoding="utf-8")
    command = python_shell(str(script), str(pidfile))
    started = time.monotonic()
    box: dict = {}

    def _call() -> None:
        box["result"] = run_command(command, cwd=tmp_path, timeout=2)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(15)
    try:
        assert not thread.is_alive(), "shell call blocked after the timeout"
        result = box["result"]
        elapsed = time.monotonic() - started
        assert elapsed < 15
        assert result.ok is False
        assert "timed_out: true" in result.output
        assert "killed after 2s" in result.output
        assert "seen-partial" in result.output
        pids = _pids(pidfile)
        assert len(pids) >= 2
        # The shell is pids[0]. The child and grandchild must be gone.
        for pid in pids[1:]:
            assert _wait_dead(pid), f"pid {pid} still alive"
    finally:
        _kill_pids(pidfile)


def test_timeout_returns_when_kill_leaves_the_pipe_open(tmp_path, monkeypatch):
    """The incident: taskkill returned, then communicate() waited on the pipe."""

    monkeypatch.setattr("local_agent.procman.kill_process_tree", lambda proc: None)
    script = tmp_path / "hold.py"
    pidfile = tmp_path / "pids.txt"
    script.write_text(_HOLD, encoding="utf-8")
    command = python_shell(str(script), str(pidfile))
    started = time.monotonic()
    box: dict = {}

    def _call() -> None:
        box["result"] = run_command(command, cwd=tmp_path, timeout=2)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(12)
    try:
        assert not thread.is_alive(), "shell call blocked while a grandchild held the pipe"
        result = box["result"]
        assert time.monotonic() - started < 12
        assert "timed_out: true" in result.output
        assert "killed after 2s" in result.output
        assert "pipes_abandoned: true" in result.output
        assert "seen-partial" in result.output
    finally:
        _kill_pids(pidfile)


def test_smoke_timeout_returns_when_a_grandchild_holds_the_pipe(tmp_path):
    script = tmp_path / "hold.py"
    pidfile = tmp_path / "pids.txt"
    script.write_text(_HOLD, encoding="utf-8")
    command = python_shell(str(script), str(pidfile))
    box: dict = {}

    def _call() -> None:
        box["result"] = run_smoke(command, cwd=tmp_path, mode="run", timeout=2)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(15)
    try:
        assert not thread.is_alive()
        assert box["result"].reason == "timeout"
        assert "timed_out: true" in box["result"].output
        assert "seen-partial" in box["result"].output
    finally:
        _kill_pids(pidfile)


def test_memory_decisions_use_mocked_readings():
    settings = MemorySettings(
        min_available_mb=512,
        min_commit_mb=1536,
        wait_seconds=10,
        poll_seconds=5,
    )
    events: list[tuple] = []

    def log(kind, **payload):
        events.append((kind, payload))

    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    ok = ensure_headroom(
        settings,
        log=log,
        where="task",
        reader=lambda: MemorySample(4000, 8000),
        sleep=sleep,
        now=now,
    )
    assert ok is True
    assert events == []
    assert clock["t"] == 0.0

    events.clear()
    readings = iter([MemorySample(100, 8000), MemorySample(4000, 8000)])
    clock["t"] = 0.0
    ok = ensure_headroom(
        settings,
        log=log,
        where="step",
        reader=lambda: next(readings),
        sleep=sleep,
        now=now,
    )
    assert ok is True
    assert [item[1]["action"] for item in events] == ["wait", "recovered"]
    assert events[0][0] == "low_memory"
    assert clock["t"] == 5

    events.clear()
    clock["t"] = 0.0
    ok = ensure_headroom(
        settings,
        log=log,
        where="checks",
        reader=lambda: MemorySample(4000, 100),
        sleep=sleep,
        now=now,
    )
    assert ok is False
    assert [item[1]["action"] for item in events] == ["wait", "pause"]
    assert clock["t"] == 10

    events.clear()
    ok = ensure_headroom(
        MemorySettings(0, 0, 0, 1),
        log=log,
        reader=lambda: (_ for _ in ()).throw(AssertionError("reader should not be called")),
    )
    assert ok is True
    assert events == []

    events.clear()
    def broken():
        raise OSError("meminfo missing")

    ok = ensure_headroom(settings, log=log, where="task", reader=broken, sleep=sleep, now=now)
    assert ok is True
    assert events[-1][1]["action"] == "unreadable"


def test_memory_settings_follow_task_then_agents_then_profile(tmp_path):
    profile = get_profile("day")
    profile.memory_min_available_mb = 100
    profile.memory_min_commit_mb = 200
    profile.memory_wait_seconds = 30
    profile.watchdog_minutes = 30
    profile.shared_compilation = False
    (tmp_path / "AGENTS.md").write_text(
        "---\n"
        "memory_min_available_mb: 50\n"
        "memory_min_commit_mb: 80\n"
        "memory_wait_seconds: 12\n"
        "watchdog_minutes: 10\n"
        "shared_compilation: true\n"
        "---\n# Rules\n",
        encoding="utf-8",
    )
    task = parse_task(
        "---\nmemory_min_available_mb: 10\nwatchdog_minutes: 4\n---\n# T\n",
    )
    apply_project_defaults(task, tmp_path)
    settings = resolve_memory_settings(task, profile)
    assert settings.min_available_mb == 10
    assert settings.min_commit_mb == 80
    assert settings.wait_seconds == 12
    assert resolve_watchdog_minutes(task, profile, None) == 4
    assert resolve_watchdog_minutes(task, profile, 1.5) == 1.5
    assert resolve_watchdog_minutes(parse_task("# bare\n"), profile, 0) == 0
    from local_agent.memory_guard import resolve_dotnet_settings

    assert resolve_dotnet_settings(task, profile) == (True, True)


def test_read_memory_returns_a_usable_sample():
    sample = read_memory()
    assert sample.available_mb > 0
    assert sample.commit_headroom_mb >= 0
    assert sample.total_physical_mb > 0


def test_error_mode_and_dotnet_env_are_configurable():
    install_error_mode()
    try:
        configure_dotnet(shared_compilation=False, shutdown_build_server=True)
        env = child_env({"PATH": "x"})
        assert env["UseSharedCompilation"] == "false"
        assert env["MSBUILDDISABLENODEREUSE"] == "1"
        assert env["DOTNET_DbgEnableMiniDump"] == "0"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        configure_dotnet(shared_compilation=True, shutdown_build_server=False)
        quiet = child_env({})
        assert "UseSharedCompilation" not in quiet
    finally:
        configure_dotnet(shared_compilation=False, shutdown_build_server=True)


def test_queue_pauses_on_low_memory_and_leaves_the_task(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write_task(
        task_dir / "01-low.md",
        "memory_min_available_mb: 999999\n"
        "memory_min_commit_mb: 999999\n"
        "memory_wait_seconds: 0\n",
    )
    _write_task(task_dir / "02-later.md", "")
    calls: list[str] = []

    def run_task(*, task, **_kwargs):
        calls.append(task.path.name)
        return _passed(task)

    result = run_queue(
        task_dir=task_dir,
        project=project,
        profile=get_profile("day"),
        client=None,
        log_dir=tmp_path / "logs",
        archive=False,
        continue_on_failure=True,
        run_task=run_task,
    )
    assert result.status == "paused_low_memory"
    assert calls == []
    assert (task_dir / "01-low.md").is_file()
    assert (task_dir / "02-later.md").is_file()
    state = load_state(result.state_path)
    assert state["tasks"]["01-low.md"]["status"] == "paused_low_memory"
    assert "02-later.md" not in state["tasks"]
    events = _events(tmp_path / "logs")
    assert any(item.get("kind") == "low_memory" and item.get("action") == "pause" for item in events)
    assert "can be run again" in result.message


def test_watchdog_kills_a_stalled_task_and_continues(tmp_path):
    project = _project(tmp_path / "repo")
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _write_task(task_dir / "01-stall.md", "")
    _write_task(task_dir / "02-next.md", "")
    calls: list[str] = []
    pidfile = project / "pids.txt"

    def run_task(*, task, project, **_kwargs):
        calls.append(task.path.name)
        if task.path.name.startswith("01"):
            script = project / "hold.py"
            script.write_text(_HOLD, encoding="utf-8")
            run_command(python_shell(str(script), str(pidfile)), cwd=project, timeout=60)
        return _passed(task)

    started = time.monotonic()
    try:
        result = run_queue(
            task_dir=task_dir,
            project=project,
            profile=get_profile("day"),
            client=None,
            log_dir=tmp_path / "logs",
            archive=False,
            run_task=run_task,
            watchdog_minutes=0.03,
        )
    finally:
        _kill_pids(pidfile)
    assert time.monotonic() - started < 20
    assert calls == ["01-stall.md", "02-next.md"]
    assert result.status == "completed_with_failures"
    state = load_state(result.state_path)
    assert state["tasks"]["01-stall.md"]["status"] == "watchdog_timeout"
    assert state["tasks"]["02-next.md"]["status"] == "passed"
    events = _events(tmp_path / "logs")
    assert any(item.get("kind") == "watchdog_timeout" for item in events)
    if pidfile.is_file():
        pids = _pids(pidfile)
        for pid in pids[1:]:
            assert _wait_dead(pid), pid


def test_step_pauses_when_memory_drops_before_the_step(tmp_path, monkeypatch):
    project = _project(tmp_path / "repo")
    seen: list[str] = []

    def fake_headroom(settings, *, log=None, where="task", **_kwargs):
        seen.append(where)
        if where == "step":
            if log is not None:
                log("low_memory", where=where, action="pause", summary="low_memory pause before step")
            return False
        return True

    monkeypatch.setattr("local_agent.memory_guard.ensure_headroom", fake_headroom)
    task = parse_task(
        "---\ntest_command: exit 0\nsteps: true\n---\n"
        "# Pause\n\n## Goal\n\nAdd nothing.\n\n"
        "## Acceptance criteria\n\n- [ ] done\n\n## Out of scope\n\n- none\n"
    )

    class Client:
        def chat(self, messages, *, tools, max_tokens):
            return ChatResult(
                content=(
                    "### Step 1\n"
                    "Goal: leave a marker\n"
                    "Files: marker.txt\n"
                    "Verify: exit 0\n"
                ),
                reasoning="",
                tool_calls=[],
                finish_reason="stop",
            )

    result = run_overnight(
        task=task,
        project=project,
        profile=get_profile("day"),
        client=Client(),
        log_dir=tmp_path / "logs",
        max_iterations=1,
        max_seconds=60,
        max_plan_turns=2,
        max_work_turns=2,
    )
    assert result.status == "paused_low_memory"
    assert "step" in seen
    text = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "low_memory" in text
    assert "paused_low_memory" in text


def _pids(path: Path) -> list[int]:
    if not path.is_file():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").split():
        try:
            found.append(int(line))
        except ValueError:
            continue
    return found


def _kill_pids(path: Path) -> None:
    # bash -c execs a simple command, so the script's parent pid is this
    # process. Never signal it.
    me = os.getpid()
    for pid in _pids(path):
        if pid == me:
            continue
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    status = Path(f"/proc/{pid}/status")
    if not status.is_file():
        return False
    try:
        text = status.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("State:"):
            return not line.split()[1].startswith("Z")
    return True


def _events(log_dir: Path) -> list[dict]:
    rows = []
    for path in Path(log_dir).rglob("events.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _project(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "integration"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return path


def _write_task(path: Path, front: str) -> None:
    head = f"---\n{front}---\n" if front.strip() else ""
    path.write_text(
        f"{head}# {path.stem}\n\n## Goal\n\nDo the work.\n\n"
        "## Acceptance criteria\n\n- [ ] done\n\n## Out of scope\n\n- none\n",
        encoding="utf-8",
    )


def _passed(task) -> OvernightResult:
    return OvernightResult(
        status="passed",
        branch="",
        report_path=None,
        commit=None,
        iterations=1,
        tests_passed=True,
        log_dir=Path("."),
        review_passed=True,
    )
