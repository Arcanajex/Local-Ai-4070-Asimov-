"""Stop request for a running task queue.

The queue reads this file only between tasks. Writing it does not interrupt
the task that is already running. A file that is already present when a queue
starts is stale: that queue warns and deletes it, then runs. Create the
request after the queue has started.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from local_agent.paths import runtime_dir

STOP_FILENAME = "STOP_QUEUE"


def default_stop_file() -> Path:
    """``runtime/STOP_QUEUE``, or ``LOCAL_AGENT_RUNTIME/STOP_QUEUE``."""

    return runtime_dir() / STOP_FILENAME


def request_stop(path: Path | None = None) -> Path:
    target = Path(path) if path is not None else default_stop_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    target.write_text(
        f"stop requested {stamp}\n"
        "The queue finishes the task that is already running, then exits.\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def cancel_stop(path: Path | None = None) -> bool:
    """Remove a stop request. Returns True when a request was waiting."""

    target = Path(path) if path is not None else default_stop_file()
    if not target.is_file():
        return False
    target.unlink()
    return True


def stop_is_requested(path: Path) -> bool:
    return Path(path).is_file()


def clear_stop_file(path: Path) -> None:
    target = Path(path)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        if target.exists():
            raise
