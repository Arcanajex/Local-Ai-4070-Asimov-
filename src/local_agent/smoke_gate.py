"""Run a task's smoke command after the unit tests pass.

`run` waits for the process and fails on a nonzero exit, a timeout, or a
Python traceback. `launch` is for a GUI or a server: start it, wait a few
seconds, treat "still running and no traceback" as success, then kill the
process tree. An early exit is a crash.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from local_agent.context import shrink_text
from local_agent.procman import kill_process_tree, popen, run_shell

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
    captured = run_shell(
        command,
        cwd=cwd,
        timeout=timeout,
        extra_env={"PYTHONUNBUFFERED": "1"},
    )
    output = _render(
        command,
        exit_code=captured.returncode,
        timed_out=captured.timed_out,
        stdout=captured.stdout,
        stderr=captured.stderr,
        pipes_abandoned=captured.pipes_abandoned,
    )
    proc_returncode = captured.returncode
    timed_out = captured.timed_out
    if timed_out:
        return SmokeResult(True, False, output, "timeout")
    if proc_returncode != 0:
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
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        for thread in threads:
            thread.join(timeout=PIPE_JOIN_SECONDS)


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


# Launch mode drains pipes itself. The join is bounded so a grandchild that
# still holds the pipe cannot stall the kill that follows a GUI smoke check.
PIPE_JOIN_SECONDS = 2.0


def _popen(command: str, cwd: Path, *, binary: bool = False) -> subprocess.Popen:
    from local_agent.procman import shell_argv

    return popen(
        shell_argv(command),
        cwd=cwd,
        env=None,
        text=not binary,
        bufsize=0 if binary else -1,
        extra_env={"PYTHONUNBUFFERED": "1"},
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
    pipes_abandoned: bool = False,
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
    if pipes_abandoned:
        lines.append("pipes_abandoned: true")
    lines.append("--- output ---")
    lines.append(combined)
    return "\n".join(lines)
