"""Agent loop: empty thinking replies, repetition, and a context overflow retry."""

from __future__ import annotations

from pathlib import Path

from local_agent.client import ChatResult, ContextOverflowError, ToolCall
from local_agent.config import get_profile
from local_agent.harness import AgentHarness
from local_agent.logging_util import RunLogger
from local_agent.tools import Workspace


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, tools, max_tokens):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _harness(tmp_path: Path, client: FakeClient, profile_name: str = "day") -> AgentHarness:
    profile = get_profile(profile_name)
    return AgentHarness(
        client,  # type: ignore[arg-type]
        Workspace(tmp_path),
        RunLogger(tmp_path / "logs"),
        profile,
    )


def _call(name: str, arguments: dict, call_id: str = "c1") -> ChatResult:
    return ChatResult(
        content="",
        reasoning="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def test_tool_then_final_answer(tmp_path):
    client = FakeClient(
        [
            _call("write", {"path": "note.txt", "content": "hello\n"}),
            ChatResult(content="wrote the note", reasoning="short", tool_calls=[], finish_reason="stop"),
        ]
    )
    result = _harness(tmp_path, client).run("make a note", max_turns=4)
    assert result.status == "completed"
    assert result.content == "wrote the note"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello\n"
    log = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
    assert "tool_result" in log
    assert "request" in log


def test_empty_length_reply_retries_with_a_larger_budget(tmp_path):
    profile = get_profile("quality")
    client = FakeClient(
        [
            ChatResult(content="", reasoning="long think", tool_calls=[], finish_reason="length"),
            ChatResult(content="the answer", reasoning="", tool_calls=[], finish_reason="stop"),
        ]
    )
    harness = AgentHarness(client, Workspace(tmp_path), RunLogger(tmp_path / "logs"), profile)  # type: ignore[arg-type]
    result = harness.run("solve it", max_turns=2)
    assert result.status == "completed"
    assert result.content == "the answer"
    assert client.calls[0]["max_tokens"] == profile.agent_max_tokens
    assert client.calls[1]["max_tokens"] == profile.agent_max_tokens_ceiling


def test_repeated_shell_call_stops_the_phase(tmp_path):
    repeated = _call("shell", {"command": "echo stuck", "timeout_seconds": 5}, "same")
    client = FakeClient([repeated, repeated, repeated, repeated, repeated])
    result = _harness(tmp_path, client).run("do the thing", max_turns=8)
    assert result.status == "loop_detected"
    # The fourth identical call is refused before it runs.
    assert len(client.calls) == 4


def test_context_overflow_compacts_and_retries(tmp_path):
    client = FakeClient(
        [
            ContextOverflowError("too long"),
            ChatResult(content="recovered", reasoning="", tool_calls=[], finish_reason="stop"),
        ]
    )
    result = _harness(tmp_path, client).run("task " + ("word " * 50), max_turns=2)
    assert result.status == "completed"
    assert result.content == "recovered"
    assert len(client.calls) == 2
