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
