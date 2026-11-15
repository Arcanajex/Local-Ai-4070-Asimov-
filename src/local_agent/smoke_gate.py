"""Run a task's smoke command after the unit tests pass.

`run` waits for the process and fails on a nonzero exit, a timeout, or a
Python traceback. `launch` is for a GUI or a server: start it, wait a few
seconds, treat "still running and no traceback" as success, then kill the
process tree. An early exit is a crash.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from local_agent.context import shrink_text
from local_agent.tools import kill_process_tree

DEFAULT_SMOKE_TIMEOUT = 120
DEFAULT_LAUNCH_SECONDS = 6.0
MAX_SMOKE_TIMEOUT = 600
MIN_LAUNCH_SECONDS = 1.0
MAX_LAUNCH_SECONDS = 60.0
SMOKE_OUTPUT_CHARS = 12_000

_TRACEBACK = "Traceback (most recent call last):"


@dataclass
class SmokeResult:
    ran: bool
    ok: bool
    output: str
    reason: str


def clamp_smoke_timeout(value: int) -> int:
    return max(1, min(int(value), MAX_SMOKE_TIMEOUT))


def clamp_launch_seconds(value: float) -> float:
    return max(MIN_LAUNCH_SECONDS, min(float(value), MAX_LAUNCH_SECONDS))


def output_has_traceback(text: str) -> bool:
    return _TRACEBACK in (text or "")


def run_smoke(
    command: str,
    *,
    cwd: Path,
    mode: str = "run",
    timeout: int = DEFAULT_SMOKE_TIMEOUT,
    launch_seconds: float = DEFAULT_LAUNCH_SECONDS,
    fail_on_traceback: bool = True,
) -> SmokeResult:
    """Execute `command` in `cwd`. An empty command does not run."""

    if not command or not command.strip():
        return SmokeResult(False, True, "", "skipped")
    kind = (mode or "run").strip().lower()
    if kind not in {"run", "launch"}:
        kind = "run"
    if kind == "launch":
        return _launch(
            command,
            cwd=cwd,
            seconds=clamp_launch_seconds(launch_seconds),
            fail_on_traceback=fail_on_traceback,
        )
    return _run(
        command,
        cwd=cwd,
        timeout=clamp_smoke_timeout(timeout),
        fail_on_traceback=fail_on_traceback,
    )


def _run(command: str, *, cwd: Path, timeout: int, fail_on_traceback: bool) -> SmokeResult:
    proc = _popen(command, cwd)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
        stdout, stderr = proc.communicate()
    output = _render(
        command,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        timed_out=timed_out,
        stdout=stdout or "",
        stderr=stderr or "",
    )
    if timed_out:
        return SmokeResult(True, False, output, "timeout")
    if proc.returncode != 0:
        return SmokeResult(True, False, output, "exit")
    if fail_on_traceback and output_has_traceback(output):
        return SmokeResult(True, False, output, "traceback")
    return SmokeResult(True, True, output, "passed")


def _launch(command: str, *, cwd: Path, seconds: float, fail_on_traceback: bool) -> SmokeResult:
    """Start `command` and watch it for `seconds`.

    The pipes are unbuffered. A buffered ``read(n)`` waits until n bytes or
    EOF, so a short traceback from a process that then sleeps would stay
    invisible until the process exited. One raw read returns as soon as the
    process writes, on Windows and on Linux. ``select`` is not used: on
    Windows it does not accept pipes.
    """

    proc = _popen(command, cwd, binary=True)
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_parts), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_parts), daemon=True),
    ]
    for thread in threads:
        thread.start()
    exited = False
    try:
        try:
            proc.wait(timeout=seconds)
            exited = True
        except subprocess.TimeoutExpired:
            exited = False
        for thread in threads:
            thread.join(timeout=0.2)
        output = _render(
            command,
            exit_code=proc.returncode if exited and proc.returncode is not None else None,
            timed_out=False,
            stdout=_decode(stdout_parts),
            stderr=_decode(stderr_parts),
            launch_seconds=seconds,
            still_running=not exited,
        )
        if fail_on_traceback and output_has_traceback(output):
            return SmokeResult(True, False, output, "traceback")
        if exited:
            return SmokeResult(True, False, output, "exited_early")
        return SmokeResult(True, True, output, "passed")
    finally:
        if proc.poll() is None:
            kill_process_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for thread in threads:
            thread.join(timeout=2)


def _drain(stream, parts: list[bytes]) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            parts.append(chunk)
    except (OSError, ValueError):
        return


def _decode(parts: list[bytes]) -> str:
    return b"".join(parts).decode("utf-8", errors="replace")


def _popen(command: str, cwd: Path, *, binary: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if os.name == "nt":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
        popen_kwargs: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        argv = ["/bin/bash", "-lc", command]
        popen_kwargs = {"start_new_session": True}
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        # Unbuffered so a launch-mode reader sees a traceback before EOF.
        bufsize=0 if binary else -1,
        env=env,
        **popen_kwargs,
    )


def _render(
    command: str,
    *,
    exit_code: int | None,
    timed_out: bool,
    stdout: str,
    stderr: str,
    launch_seconds: float | None = None,
    still_running: bool = False,
) -> str:
    combined = stdout
    if stderr:
        combined = combined + ("\n" if combined else "") + stderr
    combined = shrink_text(combined, SMOKE_OUTPUT_CHARS)
    code = "running" if exit_code is None else str(exit_code)
    lines = [f"$ {command}", f"exit_code: {code}"]
    if launch_seconds is not None:
        state = "still_running" if still_running else "exited"
        lines.append(f"launch_wait_seconds: {launch_seconds:g} ({state})")
    if timed_out:
        lines.append("timed_out: true")
    lines.append("--- output ---")
    lines.append(combined)
    return "\n".join(lines)
