"""One agent phase: prompt, tool calls, compaction, retries, stop conditions.

A phase is a fresh conversation. The overnight runner starts a new phase for
the plan, the implementation, each test-fix round, and the review. That keeps
a failure log in front of the model instead of a transcript that has already
filled the context window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from local_agent.client import (
    ChatClient,
    ChatResult,
    ContextOverflowError,
    assistant_message,
    tool_result_message,
)
from local_agent.config import Profile
from local_agent.context import compact_messages, prompt_budget
from local_agent.logging_util import RunLogger
from local_agent.loop_detect import WARNING_TEXT, LoopDetector
from local_agent.prompts import system_prompt
from local_agent.tools import Workspace, dispatch, tool_schemas


@dataclass
class PhaseResult:
    status: str
    content: str
    turns: int
    messages: list[dict[str, Any]] = field(default_factory=list)


class AgentHarness:
    def __init__(
        self,
        client: ChatClient,
        workspace: Workspace,
        logger: RunLogger,
        profile: Profile,
        *,
        allowed_tools: set[str] | None = None,
    ):
        self.client = client
        self.workspace = workspace
        self.logger = logger
        self.profile = profile
        self.allowed_tools = allowed_tools

    def run(
        self,
        user_prompt: str,
        *,
        max_turns: int,
        deadline: float | None = None,
    ) -> PhaseResult:
        schemas = tool_schemas(self.allowed_tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt},
        ]
        detector = LoopDetector()
        final_content = ""
        for turn in range(1, max_turns + 1):
            if deadline is not None and time.monotonic() >= deadline:
                self.logger.event("phase_stop", status="time_budget", summary="deadline before turn")
                return PhaseResult("time_budget", final_content, turn - 1, messages)
            result, messages = self._complete(messages, schemas)
            self.logger.event(
                "assistant",
                turn=turn,
                finish_reason=result.finish_reason,
                content=result.content,
                reasoning=result.reasoning,
                tool_calls=[
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in result.tool_calls
                ],
                usage=result.usage,
                summary=_summary(result),
            )
            if result.empty:
                nudged = self._nudge_empty(messages, schemas, result)
                if nudged is None:
                    self.logger.event("phase_stop", status="empty_reply", summary="empty after retry and nudge")
                    return PhaseResult("empty_reply", final_content, turn, messages)
                result, messages = nudged
                if result.empty:
                    self.logger.event("phase_stop", status="empty_reply", summary="empty after nudge")
                    return PhaseResult("empty_reply", final_content, turn, messages)

            verdict = detector.observe(result.tool_calls, result.content)
            if verdict == "stop":
                self.logger.event("phase_stop", status="loop_detected", summary="repeated action")
                return PhaseResult("loop_detected", result.content, turn, messages)

            if not result.tool_calls:
                final_content = result.content
                self.logger.event("phase_stop", status="completed", summary="model stopped calling tools")
                return PhaseResult("completed", final_content, turn, messages)

            if verdict == "warn":
                self.logger.event("loop_warning", summary="repeated action, one more chance")

            messages.append(assistant_message(result))
            for call in result.tool_calls:
                if self.allowed_tools is not None and call.name not in self.allowed_tools:
                    output = f"tool {call.name} is not available in this phase"
                    ok = False
                else:
                    tool_result = dispatch(call.name, call.arguments, self.workspace)
                    output = tool_result.output
                    ok = tool_result.ok
                self.logger.event(
                    "tool_result",
                    name=call.name,
                    arguments=call.arguments,
                    ok=ok,
                    output=output,
                    summary=f"{call.name} ok={ok}",
                )
                messages.append(tool_result_message(call, output))
            if verdict == "warn":
                messages.append({"role": "user", "content": WARNING_TEXT})
            final_content = result.content

        self.logger.event("phase_stop", status="turn_budget", summary=f"hit {max_turns} turns")
        return PhaseResult("turn_budget", final_content, max_turns, messages)

    def _complete(
        self,
        messages: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
    ) -> tuple[ChatResult, list[dict[str, Any]]]:
        max_tokens = self.profile.agent_max_tokens
        messages = self._fit(messages, max_tokens)
        try:
            result = self._chat(messages, schemas, max_tokens)
        except ContextOverflowError as exc:
            self.logger.event("context_overflow", summary=str(exc)[:500])
            messages = compact_messages(messages, max(1024, prompt_budget(self.profile.context, max_tokens) // 2))
            result = self._chat(messages, schemas, max_tokens)
        if result.empty and result.finish_reason == "length":
            ceiling = self.profile.agent_max_tokens_ceiling
            if ceiling > max_tokens:
                self.logger.event(
                    "empty_retry",
                    summary=f"finish_reason=length, raising max_tokens {max_tokens} -> {ceiling}",
                )
                max_tokens = ceiling
                messages = self._fit(messages, max_tokens)
                result = self._chat(messages, schemas, max_tokens)
        return result, messages

    def _nudge_empty(
        self,
        messages: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
        previous: ChatResult,
    ) -> tuple[ChatResult, list[dict[str, Any]]] | None:
        """One extra turn when thinking consumed the budget and content is empty."""

        if previous.finish_reason not in {"length", "stop", ""}:
            return None
        self.logger.event(
            "empty_nudge",
            summary="asking for a tool call or a short answer after an empty reply",
            reasoning=previous.reasoning,
        )
        messages = list(messages)
        messages.append(
            {
                "role": "user",
                "content": (
                    "The previous reply used the whole token budget and returned no answer "
                    "and no tool call. Reply now with the next tool call or a short final "
                    "answer. Keep any reasoning brief."
                ),
            }
        )
        max_tokens = self.profile.agent_max_tokens_ceiling
        messages = self._fit(messages, max_tokens)
        result = self._chat(messages, schemas, max_tokens)
        return result, messages

    def _fit(self, messages: list[dict[str, Any]], max_tokens: int) -> list[dict[str, Any]]:
        budget = prompt_budget(self.profile.context, max_tokens)
        compacted = compact_messages(messages, budget)
        if len(compacted) != len(messages):
            self.logger.event(
                "compact",
                summary=f"messages {len(messages)} -> {len(compacted)} for budget {budget}",
            )
        return compacted

    def _chat(
        self,
        messages: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
        max_tokens: int,
    ) -> ChatResult:
        self.logger.event(
            "request",
            max_tokens=max_tokens,
            message_count=len(messages),
            tools=[schema["function"]["name"] for schema in schemas],
            messages=messages,
            summary=f"chat max_tokens={max_tokens} messages={len(messages)}",
        )
        return self.client.chat(messages, tools=schemas or None, max_tokens=max_tokens)


def _summary(result: ChatResult) -> str:
    if result.tool_calls:
        names = ",".join(call.name for call in result.tool_calls)
        return f"tool_calls={names} finish={result.finish_reason}"
    text = (result.content or "").strip().replace("\n", " ")
    return f"finish={result.finish_reason} content={text[:160]}"
