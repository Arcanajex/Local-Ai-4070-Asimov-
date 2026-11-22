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
from local_agent.guardrails import (
    EXPLORATION_ESCALATION,
    PLAIN_TEXT_REMINDER,
    PhaseGuard,
    has_leaked_tool_markup,
    is_exploration_call,
    strip_leaked_tool_markup,
    tool_block_reason,
)
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
        pending_missing_reads: bool = False,
    ):
        self.client = client
        self.workspace = workspace
        self.logger = logger
        self.profile = profile
        self.allowed_tools = allowed_tools
        # Step planning reads files the step has not created yet. Those reads
        # are a result, not an error, so the model does not retry them.
        self.pending_missing_reads = pending_missing_reads

    def run(
        self,
        user_prompt: str,
        *,
        max_turns: int,
        deadline: float | None = None,
        guard: PhaseGuard | None = None,
    ) -> PhaseResult:
        guard = guard or PhaseGuard(exploration_cap=self.profile.exploration_cap)
        api_tool = bool(getattr(self.workspace, "api_lookup_enabled", False)) and (
            self.allowed_tools is None or "lookup_api" in self.allowed_tools
        )
        schemas = tool_schemas(self.allowed_tools, api_lookup=api_tool)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(guard.cheat_sheet, api_lookup=api_tool)},
            {"role": "user", "content": user_prompt},
        ]
        detector = LoopDetector()
        final_content = ""
        turn = 0
        limit = max_turns
        bonus_applied = False
        format_retries = 0
        streak = 0
        writes = 0
        explore_calls = 0
        nudged = False
        escalated = False
        refused_explores = 0
        cap = guard.exploration_cap
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                self.logger.event("phase_stop", status="time_budget", summary="deadline before turn")
                return PhaseResult("time_budget", final_content, turn, messages)
            if turn >= limit:
                if self._grant_exploration_bonus(
                    guard,
                    bonus_applied=bonus_applied,
                    writes=writes,
                    explore_calls=explore_calls,
                ):
                    bonus_applied = True
                    limit += guard.exploration_bonus_turns
                    escalated = False
                    refused_explores = 0
                    streak = 0
                    nudged = False
                    messages.append({"role": "user", "content": guard.nudge_text()})
                    self.logger.event(
                        "exploration_bonus",
                        turns=guard.exploration_bonus_turns,
                        summary=f"bonus {guard.exploration_bonus_turns} turns after exploration",
                    )
                    continue
                self.logger.event("phase_stop", status="turn_budget", summary=f"hit {max_turns} turns")
                return PhaseResult("turn_budget", final_content, turn, messages)

            turn += 1
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
                nudged_empty = self._nudge_empty(messages, schemas, result)
                if nudged_empty is None:
                    self.logger.event("phase_stop", status="empty_reply", summary="empty after retry and nudge")
                    return PhaseResult("empty_reply", final_content, turn, messages)
                result, messages = nudged_empty
                if result.empty:
                    self.logger.event("phase_stop", status="empty_reply", summary="empty after nudge")
                    return PhaseResult("empty_reply", final_content, turn, messages)

            verdict = detector.observe(result.tool_calls, result.content)
            if verdict == "stop":
                self.logger.event("phase_stop", status="loop_detected", summary="repeated action")
                return PhaseResult("loop_detected", result.content, turn, messages)

            if not result.tool_calls:
                if self._reprompt_plain_text(guard, result.content, format_retries):
                    format_retries += 1
                    if turn >= limit:
                        limit += 1
                    messages.append(
                        {"role": "assistant", "content": strip_leaked_tool_markup(result.content)}
                    )
                    messages.append({"role": "user", "content": self._plain_reminder(guard)})
                    self.logger.event("plain_text_reprompt", summary="stripped leaked tool-call markup")
                    continue
                content = result.content
                if guard.expect_plain_text:
                    content = strip_leaked_tool_markup(content).strip()
                final_content = content
                self.logger.event("phase_stop", status="completed", summary="model stopped calling tools")
                return PhaseResult("completed", final_content, turn, messages)

            if self._plain_phase_blocks_tools(guard):
                self.logger.event("plain_text_tool_rejected", summary="plain-text phase ignored tool calls")
                if self._reprompt_plain_text(guard, result.content, format_retries, tool_calls=True):
                    format_retries += 1
                    if turn >= limit:
                        limit += 1
                    messages.append(
                        assistant_message(
                            result,
                            include_reasoning=self.profile.reasoning_replay_turns > 0,
                        )
                    )
                    messages.append({"role": "user", "content": self._plain_reminder(guard)})
                    continue
                content = strip_leaked_tool_markup(result.content).strip()
                self.logger.event("phase_stop", status="completed", summary="plain-text phase stopped")
                return PhaseResult("completed", content, turn, messages)

            if verdict == "warn":
                self.logger.event("loop_warning", summary="repeated action, one more chance")

            messages.append(
                assistant_message(
                    result,
                    include_reasoning=self.profile.reasoning_replay_turns > 0,
                )
            )
            stop_exploring = False
            for call in result.tool_calls:
                output, ok, explored, wrote = self._dispatch_call(call, guard, escalated=escalated)
                if wrote:
                    streak = 0
                    writes += 1
                    escalated = False
                    refused_explores = 0
                    nudged = False
                elif explored:
                    explore_calls += 1
                    if escalated:
                        refused_explores += 1
                        if cap > 0 and refused_explores >= cap:
                            stop_exploring = True
                    else:
                        streak += 1
                else:
                    streak = 0
                self.logger.event(
                    "tool_result",
                    name=call.name,
                    arguments=call.arguments,
                    ok=ok,
                    output=output,
                    summary=f"{call.name} ok={ok}",
                )
                messages.append(tool_result_message(call, output))
                if stop_exploring:
                    break
            if stop_exploring:
                if self._grant_exploration_bonus(
                    guard,
                    bonus_applied=bonus_applied,
                    writes=writes,
                    explore_calls=explore_calls,
                ):
                    bonus_applied = True
                    limit += guard.exploration_bonus_turns
                    escalated = False
                    refused_explores = 0
                    streak = 0
                    nudged = False
                    messages.append({"role": "user", "content": guard.nudge_text()})
                    self.logger.event(
                        "exploration_bonus",
                        turns=guard.exploration_bonus_turns,
                        summary=f"bonus {guard.exploration_bonus_turns} turns after exploration cap",
                    )
                    final_content = result.content
                    continue
                self.logger.event(
                    "phase_stop",
                    status="exploration_capped",
                    summary="exploration continued after the nudge",
                )
                return PhaseResult("exploration_capped", result.content, turn, messages)
            if cap > 0 and not escalated and streak >= cap * 2:
                escalated = True
                messages.append({"role": "user", "content": EXPLORATION_ESCALATION})
                self.logger.event(
                    "exploration_escalated",
                    streak=streak,
                    summary="refusing further read-only calls until a write",
                )
            elif cap > 0 and not nudged and streak >= cap:
                nudged = True
                messages.append({"role": "user", "content": guard.nudge_text()})
                self.logger.event("exploration_nudge", streak=streak, summary=guard.nudge_text())
            if verdict == "warn":
                messages.append({"role": "user", "content": WARNING_TEXT})
            final_content = result.content

    def _plain_phase_blocks_tools(self, guard: PhaseGuard) -> bool:
        return bool(guard.expect_plain_text and self.allowed_tools is not None and len(self.allowed_tools) == 0)

    def _reprompt_plain_text(
        self,
        guard: PhaseGuard,
        content: str,
        format_retries: int,
        *,
        tool_calls: bool = False,
    ) -> bool:
        """One more plain-text try. A clean reply is not retried."""

        if not guard.expect_plain_text:
            return False
        if format_retries >= max(0, guard.plain_text_retries):
            return False
        if tool_calls:
            return True
        return has_leaked_tool_markup(content)

    def _plain_reminder(self, guard: PhaseGuard) -> str:
        return guard.plain_text_reminder.strip() or PLAIN_TEXT_REMINDER

    def _grant_exploration_bonus(
        self,
        guard: PhaseGuard,
        *,
        bonus_applied: bool,
        writes: int,
        explore_calls: int,
    ) -> bool:
        """One extra budget when the phase read and probed instead of editing."""

        return (
            not bonus_applied
            and guard.exploration_bonus_turns > 0
            and writes == 0
            and explore_calls > 0
        )

    def _dispatch_call(
        self,
        call: Any,
        guard: PhaseGuard,
        *,
        escalated: bool,
    ) -> tuple[str, bool, bool, bool]:
        """Returns output, ok, counts_as_exploration, counts_as_write."""

        explored = is_exploration_call(
            call.name,
            call.arguments,
            probe_patterns=guard.probe_patterns(),
            scaffold_patterns=guard.scaffold_patterns(),
        )
        if escalated and explored:
            return EXPLORATION_ESCALATION, False, True, False
        blocked = tool_block_reason(
            call.name,
            call.arguments,
            root=self.workspace.root,
            plan_paths=guard.plan_paths,
            probe_patterns=guard.probe_patterns(),
            scaffold_patterns=guard.scaffold_patterns(),
            scratch_markers=guard.scratch_markers(),
            project_suffixes=guard.project_suffixes(),
        )
        if blocked:
            return blocked, False, explored or call.name == "shell", False
        if self.allowed_tools is not None and call.name not in self.allowed_tools:
            return f"tool {call.name} is not available in this phase", False, explored, False
        tool_result = dispatch(
            call.name,
            call.arguments,
            self.workspace,
            missing_as_pending=self.pending_missing_reads and call.name == "read",
        )
        if call.name == "lookup_api":
            self._log_api_index()
            query = str((call.arguments or {}).get("query") or "")
            hits = 0 if tool_result.hits is None else tool_result.hits
            self.logger.event(
                "api_lookup",
                query=query,
                hits=hits,
                summary=f"api_lookup query={query} hits={hits}",
            )
        output = tool_result.output
        ok = tool_result.ok
        restored = self.workspace.restore_locked()
        if restored:
            output += (
                "\nwarning: restored locked test-first file(s): "
                + ", ".join(restored)
                + ". Do not modify those tests."
            )
            ok = False
        wrote = bool(ok and call.name in {"write", "edit"})
        return output, ok, explored and not wrote, wrote

    def _log_api_index(self) -> None:
        index = getattr(self.workspace, "_api_index", None)
        if index is None or getattr(index, "logged", False):
            return
        for warning in index.warnings:
            self.logger.event("api_index", summary=str(warning)[:500])
        self.logger.event(
            "api_index",
            packages=len(index.packages),
            members=len(index.members),
            summary=f"api_index packages={len(index.packages)} members={len(index.members)}",
        )
        index.logged = True

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
        compacted = compact_messages(
            messages,
            budget,
            reasoning_keep=self.profile.reasoning_replay_turns,
        )
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
