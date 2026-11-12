"""Keep the prompt inside the context window.

Token counts are estimated at about 4 characters per token. That is rough,
and the harness also retries once if llama-server rejects the prompt as too
long. Compaction drops whole turns, never a tool result whose tool call was
kept: the gpt-oss template errors when a tool message has no matching call.

The original task (the first user message) and the most recent turns stay.
Older tool output is the first thing to go, which is the failure mode from
the 8GB bench: a debug loop filled 32K and the server stopped.
"""

from __future__ import annotations

import copy
from typing import Any


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 8
    for message in messages:
        total += max(1, len(_message_text(message)) // 4)
    return total


def prompt_budget(context_tokens: int, generation_tokens: int, margin: int = 256) -> int:
    """Tokens the prompt may use so the completion still fits."""

    return max(1024, context_tokens - max(generation_tokens, 1) - margin)


def compact_messages(
    messages: list[dict[str, Any]],
    budget_tokens: int,
    *,
    tool_char_limit: int = 12_000,
    keep_last_groups: int = 2,
) -> list[dict[str, Any]]:
    if not messages:
        return []
    msgs = copy.deepcopy(messages)
    system, groups = _split(msgs)
    _shrink_tools(groups, tool_char_limit)
    flat = _flatten(system, groups)
    if estimate_tokens(flat) <= budget_tokens:
        return flat

    # Drop oldest groups after the original task until the budget fits.
    # Group 0 is the first user turn and is kept.
    while len(groups) > keep_last_groups + 1 and estimate_tokens(_flatten(system, groups)) > budget_tokens:
        del groups[1]

    _shrink_tools(groups, max(500, tool_char_limit // 4))
    flat = _flatten(system, groups)
    if estimate_tokens(flat) <= budget_tokens:
        return flat

    # Last resort: stub every tool result except the newest one.
    tool_seen = 0
    tool_messages = [message for group in groups for message in group if message.get("role") == "tool"]
    keep_from = max(0, len(tool_messages) - 1)
    for group in groups:
        for message in group:
            if message.get("role") != "tool":
                continue
            if tool_seen < keep_from:
                message["content"] = "[earlier tool output omitted]"
            tool_seen += 1
    return _flatten(system, groups)


def _split(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[list[dict[str, Any]]]]:
    if messages and messages[0].get("role") == "system":
        system: dict[str, Any] | None = messages[0]
        rest = messages[1:]
    else:
        system = None
        rest = messages
    groups: list[list[dict[str, Any]]] = []
    for message in rest:
        if message.get("role") == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    return system, groups


def _flatten(
    system: dict[str, Any] | None, groups: list[list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    if system is not None:
        flat.append(system)
    for group in groups:
        flat.extend(group)
    return flat


def _shrink_tools(groups: list[list[dict[str, Any]]], limit: int) -> None:
    for group in groups:
        for message in group:
            if message.get("role") == "tool" and isinstance(message.get("content"), str):
                message["content"] = shrink_text(message["content"], limit)


def shrink_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(1, limit * 2 // 3)
    marker = "\n...[truncated]...\n"
    tail = max(0, limit - head - len(marker))
    return text[:head] + marker + (text[-tail:] if tail else "")


def _message_text(message: dict[str, Any]) -> str:
    parts = [str(message.get("content") or "")]
    for call in message.get("tool_calls") or []:
        function = call.get("function") or call
        parts.append(str(function.get("name") or ""))
        parts.append(str(function.get("arguments") or ""))
    return "\n".join(parts)
