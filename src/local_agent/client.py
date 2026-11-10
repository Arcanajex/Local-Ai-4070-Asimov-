"""OpenAI-compatible chat client for llama-server, plus tool-call parsing.

llama-server with `--jinja` returns structured `tool_calls` for templates
that support them. Qwen models sometimes still emit the tool call as XML
inside `content`. Both shapes are accepted. Arguments are parsed into a
dict and sent back as a dict on later turns: the Qwen and gpt-oss Jinja
templates iterate arguments with `|items`, which does not walk a JSON string.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from local_agent.config import Profile


class ContextOverflowError(RuntimeError):
    """The server rejected the prompt because it does not fit in the context."""


class ChatError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str
    reasoning: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.tool_calls and not (self.content or "").strip()


_TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FUNCTION_BLOCK = re.compile(
    r"<function=([A-Za-z0-9_\-]+)>\s*(.*?)\s*</function>",
    re.DOTALL | re.IGNORECASE,
)
_PARAMETER = re.compile(
    r"<parameter=([A-Za-z0-9_\-]+)>\s*(.*?)\s*</parameter>",
    re.DOTALL | re.IGNORECASE,
)


def parse_tool_calls(message: dict[str, Any]) -> tuple[str, list[ToolCall]]:
    """Return visible content and tool calls from one assistant message."""

    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content)
    structured = _structured_tool_calls(message.get("tool_calls") or [])
    if structured:
        return content.strip(), structured
    fallback, stripped = _xml_tool_calls(content)
    if fallback:
        return stripped.strip(), fallback
    return content.strip(), []


def _structured_tool_calls(raw_calls: list[Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            continue
        function = item.get("function") or item
        name = function.get("name") or item.get("name")
        if not name:
            continue
        arguments = _coerce_arguments(function.get("arguments", item.get("arguments", {})))
        call_id = str(item.get("id") or f"call_{index}")
        calls.append(ToolCall(id=call_id, name=str(name), arguments=arguments))
    return calls


def _coerce_arguments(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
        if isinstance(parsed, dict):
            return parsed
        return {"_value": parsed}
    return {"_value": value}


def _xml_tool_calls(content: str) -> tuple[list[ToolCall], str]:
    calls: list[ToolCall] = []
    for match_index, match in enumerate(_TOOL_CALL_BLOCK.finditer(content)):
        body = match.group(1).strip()
        function_match = _FUNCTION_BLOCK.search(body)
        if function_match:
            name = function_match.group(1)
            params = {
                param.group(1): _maybe_json(param.group(2).strip())
                for param in _PARAMETER.finditer(function_match.group(2))
            }
            calls.append(ToolCall(id=f"call_xml_{match_index}", name=name, arguments=params))
            continue
        # Older Qwen dumps a JSON object inside the tool_call tag.
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("name"):
            arguments = payload.get("arguments", payload.get("parameters", {}))
            calls.append(
                ToolCall(
                    id=f"call_xml_{match_index}",
                    name=str(payload["name"]),
                    arguments=_coerce_arguments(arguments),
                )
            )
    if not calls:
        return [], content
    stripped = _TOOL_CALL_BLOCK.sub("", content)
    return calls, stripped


def _maybe_json(text: str) -> Any:
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def assistant_message(result: ChatResult) -> dict[str, Any]:
    """History entry for the next request.

    Reasoning is intentionally omitted. Replaying a multi-thousand-token
    thinking trace on every turn is what pushed the 35B model past 32K in
    the 8GB bench, and the gpt-oss template drops previous analysis anyway.
    """

    message: dict[str, Any] = {"role": "assistant", "content": result.content or ""}
    if result.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in result.tool_calls
        ]
    return message


def tool_result_message(call: ToolCall, output: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": output}


class ChatClient:
    """Blocking client. One request is one agent turn, which can take minutes."""

    def __init__(self, base_url: str, profile: Profile, timeout: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout = timeout if timeout is not None else profile.request_timeout_seconds

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": self.profile.filename,
            "messages": messages,
            "temperature": self.profile.temperature,
            "top_p": self.profile.top_p,
            "top_k": self.profile.top_k,
            "min_p": self.profile.min_p,
            "repeat_penalty": self.profile.repeat_penalty,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400 and _looks_like_context_error(detail):
                raise ContextOverflowError(detail) from exc
            raise ChatError(f"HTTP {exc.code}: {detail[:2000]}") from exc
        except urllib.error.URLError as exc:
            raise ChatError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        return result_from_response(raw)


def result_from_response(raw: dict[str, Any]) -> ChatResult:
    choices = raw.get("choices") or []
    if not choices:
        raise ChatError(f"response had no choices: {json.dumps(raw)[:1000]}")
    choice = choices[0]
    message = choice.get("message") or {}
    content, tool_calls = parse_tool_calls(message)
    reasoning = (
        message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
        or ""
    )
    if not isinstance(reasoning, str):
        reasoning = json.dumps(reasoning)
    return ChatResult(
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls,
        finish_reason=str(choice.get("finish_reason") or ""),
        usage=dict(raw.get("usage") or {}),
        raw=raw,
    )


def _looks_like_context_error(detail: str) -> bool:
    text = detail.lower()
    markers = (
        "context",
        "n_ctx",
        "exceeds the available context",
        "prompt is too long",
        "too many tokens",
    )
    return any(marker in text for marker in markers)


def server_is_healthy(base_url: str, timeout: int = 5) -> bool:
    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "ok" in body.lower()
    status = str(payload.get("status", "")).lower()
    return status in {"ok", "healthy", "ready"} or payload.get("ok") is True
