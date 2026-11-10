"""Stop an agent that keeps issuing the same tool call.

Local models, especially smaller ones, get stuck repeating a failed edit or
a blocking shell command. Three identical rounds produce a warning that is
fed back as a user message. A fourth identical round, or an A-B-A-B
oscillation, ends the phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LoopDetector:
    warn_after: int = 3
    stop_after: int = 4
    history: list[str] = field(default_factory=list)

    def observe(self, tool_calls: list[Any], content: str) -> str:
        """Return 'ok', 'warn', or 'stop'."""

        signature = self.signature(tool_calls, content)
        self.history.append(signature)
        streak = 0
        for previous in reversed(self.history):
            if previous != signature:
                break
            streak += 1
        if streak >= self.stop_after:
            return "stop"
        if len(self.history) >= 4:
            a, b, c, d = self.history[-4:]
            if a == c and b == d and a != b:
                return "stop"
        if streak >= self.warn_after:
            return "warn"
        return "ok"

    @staticmethod
    def signature(tool_calls: list[Any], content: str) -> str:
        if tool_calls:
            parts: list[str] = []
            for call in tool_calls:
                name = getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else "")
                arguments = getattr(call, "arguments", None)
                if arguments is None and isinstance(call, dict):
                    arguments = call.get("arguments", {})
                blob = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
                parts.append(f"{name}:{blob}")
            return "tools:" + "|".join(parts)
        collapsed = " ".join((content or "").split())
        return "text:" + collapsed[:500]


WARNING_TEXT = (
    "You are repeating the same action. Stop and do something different: "
    "read the latest error, change the arguments, or finish with a summary. "
    "Another identical action will end this phase."
)
