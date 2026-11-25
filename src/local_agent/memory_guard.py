"""Pause a task when physical memory or commit headroom is too low.

Windows reads ``GlobalMemoryStatusEx``. Linux reads ``/proc/meminfo``.
Neither imports a third-party package. A reading below the configured floor
is logged as ``low_memory``, polled until ``memory_wait_seconds``, and then
reported as a pause so the queue can stop without crashing.
"""

from __future__ import annotations

import contextvars
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

# A laptop that is about to fail a `dotnet test` with an out-of-memory dialog
# is the case these defaults are for. 0 disables that one check. Both 0 turns
# the guard off. The wait is capped so a bad config cannot stall the queue.
DEFAULT_MIN_AVAILABLE_MB = 512
DEFAULT_MIN_COMMIT_MB = 1536
DEFAULT_MEMORY_WAIT_SECONDS = 60
DEFAULT_MEMORY_POLL_SECONDS = 5.0
MEMORY_WAIT_MAX_SECONDS = 900
DEFAULT_WATCHDOG_MINUTES = 30.0

LogFn = Callable[..., None]


class LowMemoryPause(Exception):
    """Headroom stayed below the floor. The task should end as ``paused_low_memory``."""


@dataclass(frozen=True)
class MemorySample:
    available_mb: float
    commit_headroom_mb: float
    total_physical_mb: float = 0.0
    commit_limit_mb: float = 0.0


@dataclass(frozen=True)
class MemorySettings:
    min_available_mb: int
    min_commit_mb: int
    wait_seconds: float
    poll_seconds: float

    @property
    def enabled(self) -> bool:
        return self.min_available_mb > 0 or self.min_commit_mb > 0


@dataclass
class MemoryContext:
    settings: MemorySettings
    log: LogFn


_CTX: contextvars.ContextVar[MemoryContext | None] = contextvars.ContextVar(
    "asimov_memory",
    default=None,
)


def install_memory_context(settings: MemorySettings, log: LogFn) -> contextvars.Token[MemoryContext | None]:
    return _CTX.set(MemoryContext(settings, log))


def clear_memory_context(token: contextvars.Token[MemoryContext | None] | None) -> None:
    if token is not None:
        _CTX.reset(token)


def require_memory(where: str) -> None:
    """Raise ``LowMemoryPause`` when the active task is out of headroom.

    No active context (a unit test calling a check directly) does nothing.
    """

    ctx = _CTX.get()
    if ctx is None or not ctx.settings.enabled:
        return
    if not ensure_headroom(ctx.settings, log=ctx.log, where=where):
        raise LowMemoryPause(
            f"paused_low_memory before {where}: available memory or commit headroom "
            "stayed below the configured minimum"
        )


def is_low(sample: MemorySample, settings: MemorySettings) -> bool:
    if settings.min_available_mb > 0 and sample.available_mb < settings.min_available_mb:
        return True
    if settings.min_commit_mb > 0 and sample.commit_headroom_mb < settings.min_commit_mb:
        return True
    return False


def ensure_headroom(
    settings: MemorySettings,
    *,
    log: LogFn | None = None,
    where: str = "task",
    reader: Callable[[], MemorySample] = None,  # type: ignore[assignment]
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Return True when work may start. False means the caller should pause.

    ``reader``, ``sleep``, and ``now`` are injectable so tests can fake a
    reading and a clock. A failed read logs ``low_memory`` with
    ``action=unreadable`` and allows the task to continue.
    """

    if not settings.enabled:
        return True
    sample_reader = reader or read_memory
    start = now()
    deadline = start + max(0.0, min(float(settings.wait_seconds), MEMORY_WAIT_MAX_SECONDS))
    poll = max(0.05, float(settings.poll_seconds or DEFAULT_MEMORY_POLL_SECONDS))
    announced = False
    while True:
        try:
            sample = sample_reader()
        except Exception as exc:
            _emit(
                log,
                where=where,
                action="unreadable",
                summary=f"low_memory unreadable before {where}: {type(exc).__name__}: {exc}",
            )
            return True
        if not is_low(sample, settings):
            if announced:
                _emit(
                    log,
                    where=where,
                    action="recovered",
                    sample=sample,
                    settings=settings,
                    summary=f"low_memory recovered before {where}",
                )
            return True
        if not announced:
            _emit(
                log,
                where=where,
                action="wait",
                sample=sample,
                settings=settings,
                summary=(
                    f"low_memory before {where}: "
                    f"{sample.available_mb:.0f} MB available, "
                    f"{sample.commit_headroom_mb:.0f} MB commit headroom"
                ),
            )
            announced = True
        if now() >= deadline:
            _emit(
                log,
                where=where,
                action="pause",
                sample=sample,
                settings=settings,
                summary=f"low_memory pause before {where}",
            )
            return False
        remaining = deadline - now()
        sleep(min(poll, max(0.0, remaining)))


def read_memory() -> MemorySample:
    if os.name == "nt":
        return _read_windows()
    return _read_linux()


def resolve_memory_settings(task: Any, profile: Any) -> MemorySettings:
    """Task front matter, then AGENTS.md (already copied onto the task), then the profile."""

    available = _pick_int(
        getattr(task, "memory_min_available_mb", None),
        getattr(profile, "memory_min_available_mb", DEFAULT_MIN_AVAILABLE_MB),
    )
    commit = _pick_int(
        getattr(task, "memory_min_commit_mb", None),
        getattr(profile, "memory_min_commit_mb", DEFAULT_MIN_COMMIT_MB),
    )
    wait = _pick_float(
        getattr(task, "memory_wait_seconds", None),
        getattr(profile, "memory_wait_seconds", DEFAULT_MEMORY_WAIT_SECONDS),
    )
    poll = _pick_float(
        getattr(task, "memory_poll_seconds", None),
        getattr(profile, "memory_poll_seconds", DEFAULT_MEMORY_POLL_SECONDS),
    )
    return MemorySettings(
        min_available_mb=max(0, available),
        min_commit_mb=max(0, commit),
        wait_seconds=max(0.0, min(wait, MEMORY_WAIT_MAX_SECONDS)),
        poll_seconds=max(0.05, poll) if wait > 0 else poll,
    )


def resolve_watchdog_minutes(task: Any, profile: Any, explicit: float | None = None) -> float:
    """Explicit caller/CLI value, then the task, then the profile. ``<= 0`` disables."""

    if explicit is not None:
        minutes = float(explicit)
    elif getattr(task, "watchdog_minutes", None) is not None:
        minutes = float(task.watchdog_minutes)
    else:
        minutes = float(getattr(profile, "watchdog_minutes", DEFAULT_WATCHDOG_MINUTES) or 0)
    if minutes <= 0:
        return 0.0
    return minutes


def resolve_dotnet_settings(task: Any, profile: Any) -> tuple[bool, bool]:
    """``(shared_compilation, shutdown_build_server)`` with the usual precedence."""

    shared = getattr(task, "shared_compilation", None)
    if shared is None:
        shared = getattr(profile, "shared_compilation", False)
    shutdown = getattr(task, "shutdown_dotnet_build_server", None)
    if shutdown is None:
        shutdown = getattr(profile, "shutdown_dotnet_build_server", True)
    return bool(shared), bool(shutdown)


def _pick_int(task_value: int | None, profile_value: Any) -> int:
    if task_value is None:
        return int(profile_value)
    return int(task_value)


def _pick_float(task_value: float | None, profile_value: Any) -> float:
    if task_value is None:
        return float(profile_value)
    return float(task_value)


def _emit(
    log: LogFn | None,
    *,
    where: str,
    action: str,
    summary: str,
    sample: MemorySample | None = None,
    settings: MemorySettings | None = None,
) -> None:
    if log is None:
        return
    payload: dict[str, Any] = {"where": where, "action": action, "summary": summary}
    if sample is not None:
        payload["available_mb"] = round(sample.available_mb, 1)
        payload["commit_headroom_mb"] = round(sample.commit_headroom_mb, 1)
    if settings is not None:
        payload["min_available_mb"] = settings.min_available_mb
        payload["min_commit_mb"] = settings.min_commit_mb
    log("low_memory", **payload)


def _read_linux() -> MemorySample:
    values: dict[str, float] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.split()
            if not parts:
                continue
            try:
                values[key] = float(parts[0])
            except ValueError:
                continue
    available_kb = values.get("MemAvailable", values.get("MemFree", 0.0))
    limit_kb = values.get("CommitLimit", 0.0)
    committed_kb = values.get("Committed_AS", 0.0)
    headroom_kb = max(0.0, limit_kb - committed_kb)
    return MemorySample(
        available_mb=available_kb / 1024.0,
        commit_headroom_mb=headroom_kb / 1024.0,
        total_physical_mb=values.get("MemTotal", 0.0) / 1024.0,
        commit_limit_mb=limit_kb / 1024.0,
    )


def _read_windows() -> MemorySample:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(f"GlobalMemoryStatusEx failed ({ctypes.get_last_error()})")
    mb = 1024.0 * 1024.0
    return MemorySample(
        available_mb=status.ullAvailPhys / mb,
        commit_headroom_mb=status.ullAvailPageFile / mb,
        total_physical_mb=status.ullTotalPhys / mb,
        commit_limit_mb=status.ullTotalPageFile / mb,
    )
