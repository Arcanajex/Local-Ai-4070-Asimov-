"""Spawn harness commands and kill the whole tree when they time out.

A `dotnet test` run starts testhost and, unless shared compilation is off,
VBCSCompiler. Those grandchildren inherit the stdout and stderr pipes. After
the timeout the harness used to call `communicate()` with no deadline, so a
survivor that still held a pipe kept the call blocked (the Windows run that
sat for hours after `killed after 300s`).

On Windows each command is placed in a job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` before it is resumed, so closing the
job kills the process and anything it started, including a grandchild that
broke out of the Win32 parent/child tree. ``taskkill /F /T`` is the fallback
when the job cannot be created. On POSIX the command is a new session and
the timeout sends ``SIGKILL`` to that process group.

Pipe reads are threads joined with a short deadline. If they are still
blocked, the unread tail is abandoned and the partial output is returned
with ``timed_out: true``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PIPE_ABANDON_SECONDS = 2.0
# Same bound as a timeout. The join returns as soon as the readers see EOF,
# so a normal command does not wait this long. It only matters for the tail
# still sitting in the pipe after the process has exited.
_SUCCESS_DRAIN_SECONDS = PIPE_ABANDON_SECONDS

# Set on the child environment when shared compilation is disabled.
_DOTNET_QUIET = {
    "DOTNET_DbgEnableMiniDump": "0",
    "COMPlus_DbgEnableMiniDump": "0",
}

_shared_compilation = False
_shutdown_build_server = True

_tls = threading.local()


class WatchdogAbort(BaseException):
    """The queue watchdog asked the running task to stop.

    This is a ``BaseException`` so ``except Exception`` inside the overnight
    runner does not turn it into a normal task error and keep going.
    """


@dataclass
class TaskGuard:
    """Per-task abort flag and the processes that task has spawned."""

    abort: threading.Event
    processes: list[subprocess.Popen[Any]]
    lock: threading.Lock

    @classmethod
    def create(cls) -> TaskGuard:
        return cls(abort=threading.Event(), processes=[], lock=threading.Lock())


@dataclass
class Captured:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    pipes_abandoned: bool


def configure_dotnet(*, shared_compilation: bool, shutdown_build_server: bool) -> None:
    """Apply the task's dotnet build-server policy to later child processes."""

    global _shared_compilation, _shutdown_build_server
    _shared_compilation = bool(shared_compilation)
    _shutdown_build_server = bool(shutdown_build_server)


def dotnet_settings() -> tuple[bool, bool]:
    return _shared_compilation, _shutdown_build_server


def activate(guard: TaskGuard) -> None:
    _tls.guard = guard


def deactivate() -> None:
    _tls.guard = None


def current_guard() -> TaskGuard | None:
    return getattr(_tls, "guard", None)


def kill_tracked(guard: TaskGuard) -> None:
    """Kill every process the guarded task has spawned, including grandchildren."""

    with guard.lock:
        processes = list(guard.processes)
    for proc in processes:
        kill_process_tree(proc)


def install_error_mode() -> None:
    """Suppress Windows modal crash and critical-error dialogs.

    ``SetErrorMode`` is process-wide and inherited by children created
    afterwards. ``SEM_NOGPFAULTERRORBOX`` is what stops the "Unknown Hard
    Error" dialog for a child that dies with a fatal exception. No registry
    write and no admin rights.
    """

    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    SEM_FAILCRITICALERRORS = 0x0001
    SEM_NOGPFAULTERRORBOX = 0x0002
    SEM_NOOPENFILEERRORBOX = 0x8000
    flags = SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetErrorMode.argtypes = [wintypes.UINT]
    kernel32.SetErrorMode.restype = wintypes.UINT
    try:
        kernel32.GetErrorMode.restype = wintypes.UINT
        current = int(kernel32.GetErrorMode())
    except AttributeError:
        current = int(kernel32.SetErrorMode(flags))
    kernel32.SetErrorMode(current | flags)


def child_env(base: dict[str, str] | None = None, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a harness child.

    Mini-dump and crash-dialog env vars default off. When shared compilation
    is disabled, MSBuild is told not to leave VBCSCompiler or the MSBuild
    node server running after the command.
    """

    env = dict(os.environ if base is None else base)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key, value in _DOTNET_QUIET.items():
        env.setdefault(key, value)
    if not _shared_compilation:
        env["UseSharedCompilation"] = "false"
        env["MSBUILDDISABLENODEREUSE"] = "1"
        env["DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER"] = "1"
    if extra:
        env.update(extra)
    return env


def shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/bash", "-lc", command]


def popen(
    argv: list[str],
    *,
    cwd: Path | str,
    env: dict[str, str] | None = None,
    text: bool = True,
    bufsize: int = -1,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[Any]:
    """Start ``argv`` in a killable tree. The child inherits the error mode."""

    install_error_mode()
    merged = child_env(env, extra=extra_env)
    proc = _spawn(argv, cwd=cwd, env=merged, text=text, bufsize=bufsize)
    _track(proc)
    return proc


def run_shell(
    command: str,
    *,
    cwd: Path | str,
    timeout: float,
    extra_env: dict[str, str] | None = None,
) -> Captured:
    env = child_env(extra=extra_env)
    return run_captured(
        shell_argv(command),
        cwd=cwd,
        timeout=timeout,
        env=env,
        shutdown_dotnet=command_mentions_dotnet(command),
    )


def run_captured(
    argv: list[str],
    *,
    cwd: Path | str,
    timeout: float,
    env: dict[str, str] | None = None,
    shutdown_dotnet: bool = False,
) -> Captured:
    """Run ``argv`` and return even if a grandchild keeps the pipes open."""

    # Unbuffered binary reads return as soon as the child writes. A text-mode
    # read(n) waits for n characters or EOF, which hides a short line until
    # the pipe closes and makes a timeout look like it produced no output.
    proc = popen(argv, cwd=cwd, env=env, text=False, bufsize=0)
    stdout_parts: list[Any] = []
    stderr_parts: list[Any] = []
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_parts), name="asimov-stdout", daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_parts), name="asimov-stderr", daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=max(0.0, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
    abandoned = _finish_readers(proc, threads, timed_out=timed_out)
    code = proc.poll()
    if code is None:
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            code = -1
        else:
            code = proc.returncode if proc.returncode is not None else -1
    if code is None:
        code = -1
    # Reap a Roslyn/MSBuild server that outlived the command. Skip it when the
    # watchdog is already tearing the task down; that path has its own deadline.
    if timed_out and shutdown_dotnet and not _abort_requested():
        shutdown_dotnet_build_server()
    _maybe_abort()
    return Captured(
        returncode=int(code),
        stdout=_join_chunks(stdout_parts, text=False),
        stderr=_join_chunks(stderr_parts, text=False),
        timed_out=timed_out,
        pipes_abandoned=abandoned,
    )


def kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill ``proc`` and every process it started. Never waits on its pipes."""

    if os.name == "nt":
        _terminate_job(proc)
        _taskkill(proc.pid)
        _release_job(proc)
        return
    _kill_group(proc)


def shutdown_dotnet_build_server() -> None:
    """Best-effort ``dotnet build-server shutdown``. Missing dotnet is ignored."""

    if not _shutdown_build_server:
        return
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return
    try:
        subprocess.run(
            [dotnet, "build-server", "shutdown"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _finish_readers(
    proc: subprocess.Popen[Any],
    threads: list[threading.Thread],
    *,
    timed_out: bool,
) -> bool:
    grace = PIPE_ABANDON_SECONDS if timed_out else _SUCCESS_DRAIN_SECONDS
    _join(threads, grace)
    if not _threads_alive(threads):
        # Readers saw EOF. Drop the job so a build server still inside it dies
        # with the command instead of lingering until this process exits.
        if not timed_out:
            _release_job(proc)
        return False
    # The process is gone, or should be, but something still holds a pipe.
    # Closing the pipe from this thread deadlocks the thread blocked in read(),
    # so the readers are daemon threads and this returns with whatever they
    # already copied out.
    kill_process_tree(proc)
    _join(threads, 0.3)
    return _threads_alive(threads)


def _spawn(
    argv: list[str],
    *,
    cwd: Path | str,
    env: dict[str, str],
    text: bool,
    bufsize: int,
) -> subprocess.Popen[Any]:
    if os.name == "nt":
        return _spawn_windows(argv, cwd=cwd, env=env, text=text, bufsize=bufsize)
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        bufsize=bufsize,
        start_new_session=True,
    )


def _spawn_windows(
    argv: list[str],
    *,
    cwd: Path | str,
    env: dict[str, str],
    text: bool,
    bufsize: int,
) -> subprocess.Popen[Any]:
    import ctypes
    from ctypes import wintypes

    CREATE_SUSPENDED = 0x00000004
    job = _create_job()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if job:
        flags |= CREATE_SUSPENDED
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            bufsize=bufsize,
            creationflags=flags,
        )
    except Exception:
        if job:
            _close_handle(job)
        raise
    if not job:
        return proc
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    assigned = bool(kernel32.AssignProcessToJobObject(job, proc._handle))
    resumed = _resume_process(proc)
    if assigned and resumed:
        proc._asimov_job = job  # type: ignore[attr-defined]
    else:
        _close_handle(job)
        if not resumed:
            proc.kill()
    return proc


def _create_job() -> int | None:
    import ctypes
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        _close_handle(job)
        return None
    return int(job)


def _resume_process(proc: subprocess.Popen[Any]) -> bool:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    resume = ntdll.NtResumeProcess
    resume.argtypes = [wintypes.HANDLE]
    resume.restype = ctypes.c_ulong
    status = int(resume(proc._handle))
    return status < 0x80000000


def _terminate_job(proc: subprocess.Popen[Any]) -> None:
    job = getattr(proc, "_asimov_job", None)
    if not job:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject(job, 1)


def _release_job(proc: subprocess.Popen[Any]) -> None:
    job = getattr(proc, "_asimov_job", None)
    if not job:
        return
    proc._asimov_job = None  # type: ignore[attr-defined]
    _close_handle(job)


def _close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            return


def _track(proc: subprocess.Popen[Any]) -> None:
    guard = current_guard()
    if guard is None:
        return
    with guard.lock:
        guard.processes.append(proc)


def _abort_requested() -> bool:
    guard = current_guard()
    return guard is not None and guard.abort.is_set()


def _maybe_abort() -> None:
    if _abort_requested():
        raise WatchdogAbort("watchdog_timeout")


def _drain(stream: Any, parts: list[Any]) -> None:
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


def _join(threads: list[threading.Thread], seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(timeout=remaining)


def _threads_alive(threads: list[threading.Thread]) -> bool:
    return any(thread.is_alive() for thread in threads)


def _join_chunks(parts: list[Any], *, text: bool) -> str:
    if not parts:
        return ""
    if text:
        return "".join(str(part) for part in parts)
    return b"".join(parts).decode("utf-8", errors="replace")


def command_mentions_dotnet(command: str) -> bool:
    return "dotnet" in (command or "").lower()
