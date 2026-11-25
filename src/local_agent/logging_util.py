"""JSONL log of every request, response, tool call, and phase change.

The log is the record of an unattended run. It stores the messages that
were actually sent (after compaction) and the raw server payload, including
reasoning text that is not replayed into the next prompt.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def append_event(directory: Path, kind: str, **payload: Any) -> None:
    """Append one event without holding the log file open.

    The queue watchdog and the memory guard write here from outside the task's
    own ``RunLogger``, which may already have the attempt log open.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **payload,
    }
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    summary = payload.get("summary") or payload.get("status") or ""
    with (directory / "events.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{record['ts']} {kind} {summary}\n")
        handle.flush()


class RunLogger:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.directory / "events.jsonl"
        self.text_path = self.directory / "events.log"
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        self._text = self.text_path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._jsonl.close()
        self._text.close()

    def event(self, kind: str, **payload: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **payload,
        }
        self._jsonl.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._jsonl.flush()
        summary = payload.get("summary") or payload.get("status") or ""
        self._text.write(f"{record['ts']} {kind} {summary}\n")
        self._text.flush()
