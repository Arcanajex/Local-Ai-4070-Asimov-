"""Tool-call parsing, context compaction, and loop detection."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_agent.client import (
    ChatClient,
    ContextOverflowError,
    assistant_message,
    parse_tool_calls,
    result_from_response,
)
from local_agent.config import get_profile
from local_agent.context import compact_messages, estimate_tokens, prompt_budget
from local_agent.loop_detect import LoopDetector


def test_openai_tool_call_and_reasoning_are_split():
    raw = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "I should read the file.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{\"path\": \"adder.py\"}"},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    result = result_from_response(raw)
    assert result.reasoning.startswith("I should read")
    assert result.tool_calls[0].name == "read"
    assert result.tool_calls[0].arguments == {"path": "adder.py"}
    history = assistant_message(result)
    assert "reasoning" not in history
    assert history["tool_calls"][0]["function"]["arguments"] == {"path": "adder.py"}


def test_qwen_xml_tool_call_in_content():
    content = (
        "I'll read it.\n"
        "<tool_call>\n"
        "<function=read>\n"
        "<parameter=path>\n"
        "adder.py\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>\n"
    )
    visible, calls = parse_tool_calls({"content": content})
    assert calls[0].name == "read"
    assert calls[0].arguments["path"] == "adder.py"
    assert "<tool_call>" not in visible
    assert "I'll read it." in visible


def test_json_tool_call_inside_xml():
    content = '<tool_call>\n{"name": "shell", "arguments": {"command": "echo hi"}}\n</tool_call>'
    _visible, calls = parse_tool_calls({"content": content})
    assert calls[0].name == "shell"
    assert calls[0].arguments["command"] == "echo hi"


def test_context_error_from_the_server(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"error": {"message": "request exceeds the available context size"}}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        profile = get_profile("day")
        client = ChatClient(f"http://127.0.0.1:{httpd.server_address[1]}", profile, timeout=5)
        with pytest.raises(ContextOverflowError):
            client.chat([{"role": "user", "content": "hi"}], tools=None, max_tokens=16)
    finally:
        httpd.shutdown()


def test_compaction_drops_old_turns_and_keeps_tool_pairs():
    messages = [{"role": "system", "content": "system rules"}, {"role": "user", "content": "TASK build the adder"}]
    for index in range(10):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "shell", "arguments": {"command": f"echo {index}"}}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "OUTPUT " + ("x" * 5000)})
    budget = prompt_budget(40960, 16384)
    assert estimate_tokens(messages) > 2000
    compacted = compact_messages(messages, budget_tokens=800)
    assert estimate_tokens(compacted) <= 800 or compacted[-1]["role"] == "tool"
    assert compacted[0]["role"] == "system"
    assert any("TASK build the adder" in str(message.get("content")) for message in compacted)
    assert "OUTPUT" in compacted[-1]["content"]
    # A tool message must follow an assistant or another tool message.
    for index, message in enumerate(compacted):
        if message["role"] != "tool":
            continue
        assert compacted[index - 1]["role"] in {"assistant", "tool"}


def test_loop_detector_warns_then_stops_and_catches_oscillation():
    detector = LoopDetector()
    call = type("Call", (), {"name": "shell", "arguments": {"command": "echo hi"}})()
    assert detector.observe([call], "") == "ok"
    assert detector.observe([call], "") == "ok"
    assert detector.observe([call], "") == "warn"
    assert detector.observe([call], "") == "stop"

    other = LoopDetector()
    a = type("Call", (), {"name": "read", "arguments": {"path": "a"}})()
    b = type("Call", (), {"name": "read", "arguments": {"path": "b"}})()
    assert other.observe([a], "") == "ok"
    assert other.observe([b], "") == "ok"
    assert other.observe([a], "") == "ok"
    assert other.observe([b], "") == "stop"
