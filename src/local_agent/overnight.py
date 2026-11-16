"""Quality-focused overnight loop.

The task file goes in, then the model plans, implements, and the runner
itself executes the test command. Failures are handed back as a new, short
conversation. When tests pass or the iteration or time budget is spent, the
model reviews the diff with no tools. The runner commits the branch and
writes AGENT_REPORT.md. The model is not asked to commit: an unattended
`git commit` from the shell is how a run leaves the tree in a surprise state.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from local_agent.client import ChatClient
from local_agent.config import Profile, get_profile
from local_agent.context import shrink_text
from local_agent.guardrails import (
    DEFAULT_API_MISUSE,
    DEFAULT_CHEAT_MARKERS,
    DEFAULT_EXPLORATION_CAP,
    DEFAULT_IMPLEMENT_TURNS,
    DEFAULT_PLAN_TURNS,
    PLAN_EXPLORATION_NUDGE,
    PhaseGuard,
    collect_cheat_sheets,
    false_pass_release,
    implementation_gap,
    looks_like_api_misuse,
    missing_must_exist,
    planned_paths,
    resolve_optional_int,
    resolve_turn_budget,
    strip_leaked_tool_markup,
)
from local_agent.harness import AgentHarness, PhaseResult
from local_agent.logging_util import RunLogger
from local_agent.project_context import (
    DEFAULT_CONTEXT_CHARS,
    build_project_context,
    shrink_context_to_budget,
    with_project_context,
)
from local_agent.prompts import (
    fix_prompt,
    implement_prompt,
    plan_prompt,
    review_fix_prompt,
    review_format_reminder,
    review_prompt,
    test_first_prompt,
    test_first_repair_prompt,
)
from local_agent.snapshots import Snapshot, create_snapshot, restore_snapshot
from local_agent.repo_map import build_repo_map
from local_agent.smoke_gate import run_smoke
from local_agent.taskcheck import (
    extract_section,
    format_problems,
    review_form_problems,
    review_problems,
    validate_task,
)
from local_agent.test_first import (
    changed_rels,
    classify_test_run,
    file_digest,
    is_test_path,
    restore_rels,
    snapshot_tree,
)
from local_agent.tools import Workspace, clear_bytecode, run_command
from local_agent.traceback_files import format_traceback_files

PLAN_FILE = "AGENT_PLAN.md"
REPORT_FILE = "AGENT_REPORT.md"
AGENT_DIR_NAME = ".agent"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# 32_000 characters is about a third more than the old 24_000 cap, not the
# full 1.6x of the 40K -> 64K context change.
REVIEW_DIFF_CHARS = 32_000
FIX_DIFF_CHARS = 8_000
RETRYABLE_STATUSES = {
    "iteration_budget",
    "loop_detected",
    "review_failed",
    "false_pass",
    "review_malformed",
    "review_fix_reverted",
    "restored_snapshot",
}
_LIST_KEYS = {"context", "must_exist", "blocked_probes", "api_error_patterns", "scratch_dirs"}
_FRONT_MATTER_ERROR = (
    "front matter could not be parsed: the file starts with --- but there is no closing ---. "
    "A UTF-8 BOM is stripped before parsing; the closing line must be exactly ---."
)


@dataclass
class TaskSpec:
    body: str
    title: str
    test_command: str
    lint_command: str | None
    path: Path | None = None
    test_command_explicit: bool = False
    context_paths: list[str] = field(default_factory=list)
    smoke_command: str | None = None
    smoke_mode: str = "run"
    smoke_timeout: int = 120
    smoke_launch_seconds: float = 6.0
    smoke_fail_on_traceback: bool = True
    test_first: bool = False
    explicit_keys: set[str] = field(default_factory=set)
    must_exist: list[str] = field(default_factory=list)
    plan_turns: int | None = None
    implement_turns: int | None = None
    exploration_cap: int | None = None
    exploration_bonus_turns: int | None = None
    cheat_sheet_marker: str = ""
    blocked_probes: list[str] = field(default_factory=list)
    api_error_patterns: list[str] = field(default_factory=list)
    scratch_dirs: list[str] = field(default_factory=list)
    front_matter_error: str | None = None


@dataclass
class OvernightResult:
    status: str
    branch: str
    report_path: Path | None
    commit: str | None
    iterations: int
    tests_passed: bool
    log_dir: Path
    review_passed: bool = True
    smoke_passed: bool = True
    smoke_ran: bool = False
    attempts: int = 1
    attempt_history: list[dict[str, Any]] = field(default_factory=list)
    stop_requested: bool = False
    test_output: str = ""
    review: str = ""
    snapshot_refs: list[str] = field(default_factory=list)
    restored_snapshot: str = ""


def parse_task(text: str, path: Path | None = None) -> TaskSpec:
    meta, lists, body, error = _split_front_matter(text)
    title = _title(body) or (path.stem if path else "task")
    explicit = "test_command" in meta
    test_command = str(meta["test_command"]) if explicit else "python -m pytest -q"
    lint_raw = meta.get("lint_command")
    lint_command = str(lint_raw) if lint_raw else None
    smoke_mode = (meta.get("smoke_mode") or "run").strip().lower()
    if smoke_mode not in {"run", "launch"}:
        smoke_mode = "run"
    return TaskSpec(
        body=body.strip(),
        title=title,
        test_command=test_command,
        lint_command=lint_command,
        path=path,
        test_command_explicit=explicit,
        context_paths=lists.get("context", []),
        smoke_command=meta.get("smoke_command") or None,
        smoke_mode=smoke_mode,
        smoke_timeout=_meta_int(meta.get("smoke_timeout"), 120),
        smoke_launch_seconds=_meta_float(meta.get("smoke_launch_seconds"), 6.0),
        smoke_fail_on_traceback=(
            _truthy(meta["smoke_fail_on_traceback"]) if "smoke_fail_on_traceback" in meta else True
        ),
        test_first=_truthy(meta["test_first"]) if "test_first" in meta else False,
        explicit_keys=set(meta) | {key for key, values in lists.items() if values},
        must_exist=lists.get("must_exist", []),
        plan_turns=_optional_int(meta, "plan_turns"),
        implement_turns=_optional_int(meta, "implement_turns"),
        exploration_cap=_optional_int(meta, "exploration_cap"),
        exploration_bonus_turns=_optional_int(meta, "exploration_bonus_turns"),
        cheat_sheet_marker=meta.get("cheat_sheet_marker", ""),
        blocked_probes=lists.get("blocked_probes", []),
        api_error_patterns=lists.get("api_error_patterns", []),
        scratch_dirs=lists.get("scratch_dirs", []),
        front_matter_error=error,
    )


def load_task(path: Path) -> TaskSpec:
    # utf-8-sig drops a leading BOM. A BOM used to hide the front matter, and
    # validate then reported a missing test_command instead of the real cause.
    return parse_task(Path(path).read_text(encoding="utf-8-sig"), Path(path))


def _split_front_matter(text: str) -> tuple[dict[str, str], dict[str, list[str]], str, str | None]:
    text = text.lstrip("\ufeff")
    if text.startswith("---"):
        match = _FRONT_MATTER.match(text)
        if not match:
            return {}, {key: [] for key in _LIST_KEYS}, text, _FRONT_MATTER_ERROR
    else:
        match = None
    if not match:
        return {}, {key: [] for key in _LIST_KEYS}, text, None
    meta, lists = _parse_front_matter(match.group(1))
    return meta, lists, text[match.end() :], None


def _parse_front_matter(block: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Line-oriented front matter, including list keys such as `context` and `must_exist`."""

    meta: dict[str, str] = {}
    lists: dict[str, list[str]] = {key: [] for key in _LIST_KEYS}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in _LIST_KEYS and not value:
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith("- "):
                item = lines[index].lstrip()[2:].strip().strip('"').strip("'")
                if item:
                    lists[key].append(item)
                index += 1
            continue
        if key in _LIST_KEYS:
            lists[key].extend(part.strip() for part in value.split(",") if part.strip())
        else:
            meta[key] = value
        index += 1
    return meta, lists


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _meta_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _meta_float(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _optional_int(meta: dict[str, str], key: str) -> int | None:
    if key not in meta or str(meta.get(key) or "").strip() == "":
        return None
    try:
        return int(float(meta[key]))
    except (TypeError, ValueError):
        return None


def agents_front_matter(project: Path) -> dict[str, str]:
    """Front matter of the target repo's AGENTS.md, or {} when it has none."""

    path = Path(project) / "AGENTS.md"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    meta, _lists, _body, error = _split_front_matter(text)
    if error:
        return {}
    return meta


def apply_project_defaults(task: TaskSpec, project: Path) -> TaskSpec:
    """Fill smoke and test-first settings the task file left unset.

    A key present on the task, even with an empty value, wins over AGENTS.md.
    """

    meta = agents_front_matter(project)
    if "smoke_command" not in task.explicit_keys and meta.get("smoke_command"):
        task.smoke_command = meta["smoke_command"]
    if "smoke_mode" not in task.explicit_keys and meta.get("smoke_mode"):
        mode = meta["smoke_mode"].strip().lower()
        if mode in {"run", "launch"}:
            task.smoke_mode = mode
    if "smoke_timeout" not in task.explicit_keys and meta.get("smoke_timeout"):
        task.smoke_timeout = _meta_int(meta.get("smoke_timeout"), task.smoke_timeout)
    if "smoke_launch_seconds" not in task.explicit_keys and meta.get("smoke_launch_seconds"):
        task.smoke_launch_seconds = _meta_float(meta.get("smoke_launch_seconds"), task.smoke_launch_seconds)
    if "smoke_fail_on_traceback" not in task.explicit_keys and "smoke_fail_on_traceback" in meta:
        task.smoke_fail_on_traceback = _truthy(meta["smoke_fail_on_traceback"])
    if "test_first" not in task.explicit_keys and "test_first" in meta:
        task.test_first = _truthy(meta["test_first"])
    if task.plan_turns is None and "plan_turns" in meta:
        task.plan_turns = _optional_int(meta, "plan_turns")
    if task.implement_turns is None and "implement_turns" in meta:
        task.implement_turns = _optional_int(meta, "implement_turns")
    if task.exploration_cap is None and "exploration_cap" in meta:
        task.exploration_cap = _optional_int(meta, "exploration_cap")
    if task.exploration_bonus_turns is None and "exploration_bonus_turns" in meta:
        task.exploration_bonus_turns = _optional_int(meta, "exploration_bonus_turns")
    if not task.cheat_sheet_marker and meta.get("cheat_sheet_marker"):
        task.cheat_sheet_marker = meta["cheat_sheet_marker"]
    return task


def _title(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "task"


def run_overnight(
    *,
    task: TaskSpec,
    project: Path,
    profile: Profile,
    client: ChatClient,
    log_dir: Path,
    max_iterations: int = 6,
    max_seconds: float = 8 * 3600,
    max_plan_turns: int = 8,
    max_work_turns: int = 30,
    test_command: str | None = None,
    lint_command: str | None = None,
    init_repo: bool = False,
    strict: bool = True,
    context_char_limit: int = DEFAULT_CONTEXT_CHARS,
    test_first: bool | None = None,
    prior_attempt: str = "",
    branch_attempt: int = 0,
) -> OvernightResult:
    project = Path(project).resolve()
    logger = RunLogger(log_dir)
    started = time.monotonic()
    deadline = started + max_seconds
    test_cmd = test_command or task.test_command
    lint_cmd = lint_command if lint_command is not None else task.lint_command
    branch = ""
    iterations = 0
    tests_passed = False
    test_output = ""
    plan_status = "not_started"
    identity_note = ""
    review_passed = True
    smoke_passed = True
    smoke_ran = False
    context_block = ""
    plan_text = ""
    review_text = ""
    use_test_first = False
    test_first_files = ""
    snapshots: list[Snapshot] = []
    restored_snapshot = ""
    restore_note = {"ref": ""}
    try:
        apply_project_defaults(task, project)
        max_plan_turns = resolve_turn_budget(
            max_plan_turns, DEFAULT_PLAN_TURNS, task.plan_turns, profile.plan_turns
        )
        max_work_turns = resolve_turn_budget(
            max_work_turns, DEFAULT_IMPLEMENT_TURNS, task.implement_turns, profile.implement_turns
        )
        exploration_cap = resolve_optional_int(
            task.exploration_cap,
            profile.exploration_cap if profile.exploration_cap is not None else DEFAULT_EXPLORATION_CAP,
        )
        exploration_bonus = resolve_optional_int(task.exploration_bonus_turns, profile.exploration_bonus_turns)
        use_test_first = task.test_first if test_first is None else bool(test_first)
        identity_note = ensure_repository(project, init_repo=init_repo) or ""
        if identity_note:
            logger.event("git_identity", summary=identity_note)
            print(identity_note, file=sys.stderr)
        _require_clean_git(project)
        exclude_agent_dir(project)
        if strict:
            problems = validate_task(task, project)
            if problems:
                raise RuntimeError(format_problems(problems))
        workspace = Workspace(project)
        context_block = build_project_context(project, task.context_paths, limit=context_char_limit)
        repo_map = build_repo_map(project)
        if repo_map.strip():
            context_block = (context_block.rstrip() + "\n\n" + repo_map).strip() + "\n"
        if prior_attempt.strip():
            context_block = (
                context_block.rstrip()
                + "\n\nPrevious attempt\n\n"
                + "An earlier attempt on a fresh branch failed. Tests are still the judge. "
                + "Do not repeat an approach that already failed these checks.\n\n"
                + prior_attempt.strip()
                + "\n"
            )
        context_block = shrink_context_to_budget(
            context_block,
            context_tokens=profile.context,
            generation_tokens=profile.agent_max_tokens_ceiling,
        )
        logger.event(
            "project_context",
            chars=len(context_block),
            paths=["AGENTS.md", *task.context_paths],
            summary=f"project context {len(context_block)} chars",
        )
        branch = _create_branch(project, slugify(task.title), attempt=branch_attempt)
        # The commit the task started from. Later agent commits move HEAD, so a
        # diff against HEAD would only see the last round and look empty.
        base_sha = _git(project, "rev-parse", "HEAD").strip() if _head_exists(project) else ""
        logger.event("task_base", commit=base_sha, summary=f"task base {base_sha[:12] or '(none)'}")
        markers = list(profile.cheat_sheet_markers) or list(DEFAULT_CHEAT_MARKERS)
        if task.cheat_sheet_marker and task.cheat_sheet_marker.lower() not in {item.lower() for item in markers}:
            markers.append(task.cheat_sheet_marker)
        cheat_sheet = collect_cheat_sheets(project, task.context_paths, markers)
        api_patterns = tuple(profile.api_misuse_patterns) or DEFAULT_API_MISUSE
        if task.api_error_patterns:
            api_patterns = tuple(dict.fromkeys((*api_patterns, *task.api_error_patterns)))
        extra_probes = tuple(dict.fromkeys((*profile.extra_probe_patterns, *task.blocked_probes)))
        extra_scratch = tuple(dict.fromkeys((*profile.extra_scratch_markers, *task.scratch_dirs)))
        logger.event(
            "run_start",
            branch=branch,
            profile=profile.name,
            task=task.title,
            plan_turns=max_plan_turns,
            implement_turns=max_work_turns,
            exploration_cap=exploration_cap,
            cheat_sheet_chars=len(cheat_sheet),
            summary=branch,
        )
        plan_guard = PhaseGuard(
            exploration_cap=exploration_cap,
            exploration_bonus_turns=exploration_bonus,
            expect_plain_text=True,
            plain_text_retries=2,
            nudge=PLAN_EXPLORATION_NUDGE,
            extra_probe_patterns=extra_probes,
            extra_scratch_markers=extra_scratch,
        )

        plan = _run_phase(
            client, workspace, logger, profile,
            with_project_context(plan_prompt(task.body), context_block),
            allowed={"read"},
            max_turns=max_plan_turns,
            deadline=deadline,
            phase="plan",
            guard=plan_guard,
        )
        plan_status = plan.status
        plan_text = strip_leaked_tool_markup(plan.content).strip() or "(the model did not return a plan)"
        _write_agent_file(project, PLAN_FILE, _plan_document(task, plan_text))
        plan_file_paths = tuple(planned_paths(plan_text))
        logger.event(
            "plan_paths",
            paths=list(plan_file_paths),
            summary=", ".join(plan_file_paths[:12]) or "(none)",
        )
        probe_guard = PhaseGuard(
            exploration_cap=exploration_cap,
            exploration_bonus_turns=exploration_bonus,
            plan_paths=plan_file_paths,
            extra_probe_patterns=extra_probes,
            extra_scratch_markers=extra_scratch,
        )
        work_guard = replace(probe_guard, cheat_sheet=cheat_sheet)

        if use_test_first and time.monotonic() < deadline:
            test_first_files = _run_test_first(
                client, workspace, logger, profile, task, project, context_block, deadline, max_work_turns, test_cmd,
                guard=probe_guard,
            )

        if time.monotonic() >= deadline:
            return _finish(
                project, logger, task, profile, branch, log_dir,
                status="time_budget", iterations=0, tests_passed=False,
                test_output="", review="Stopped before implementation because the time budget was spent.",
                plan_status=plan.status,
                identity_note=identity_note,
            )

        implement = _run_phase(
            client, workspace, logger, profile,
            with_project_context(implement_prompt(task.body, plan_text), context_block),
            allowed=None,
            max_turns=max_work_turns,
            deadline=deadline,
            phase="implement",
            guard=work_guard,
        )
        tests_passed = False
        test_output = ""
        iterations = 0
        stop_status = implement.status
        # Sticky across a real edit. A later no-op does not clear it: the gate
        # that follows a round which changed files is success even if the plan
        # matcher is still unhappy. The no-op streak only counts rounds that
        # ran after a passing gate, which is when the guard is the thing looping.
        fix_produced_changes = False
        consecutive_noop_fixes = 0
        # Set when a fix round's edits, or the two-noop valve, accept a gate
        # the cumulative diff still calls a gap. A clean diff is re-checked
        # after review-fix, because that later phase can undo the tree.
        accept_remaining_gap = False

        def cumulative_gap() -> str | None:
            return implementation_gap(plan_text, working_tree_diff(project, base_sha))

        def release_guard(reason: str, *, accept_gap: bool) -> None:
            nonlocal stop_status, accept_remaining_gap
            if accept_gap:
                accept_remaining_gap = True
            _log_guard(logger, "release", iterations, reason, implement_status=implement.status, base=base_sha[:12])
            _record_snapshot(project, branch, snapshots, logger)
            stop_status = "passed"

        def note_fix_edits(before: dict[str, bytes]) -> bool:
            nonlocal fix_produced_changes, consecutive_noop_fixes
            changed = bool(changed_rels(before, snapshot_tree(project)))
            if changed:
                fix_produced_changes = True
                consecutive_noop_fixes = 0
            return changed

        for iterations in range(1, max_iterations + 1):
            if time.monotonic() >= deadline:
                stop_status = "time_budget"
                break
            test_output, tests_passed, smoke_passed, ran_smoke = _run_iteration_checks(
                project, test_cmd, lint_cmd, task, logger, iterations
            )
            smoke_ran = smoke_ran or ran_smoke
            if tests_passed and smoke_passed:
                gap_reason = cumulative_gap()
                release = false_pass_release(
                    gap_reason,
                    fix_produced_changes=fix_produced_changes,
                    consecutive_noop_fixes=consecutive_noop_fixes,
                    implement_status=implement.status,
                )
                if release:
                    reason, accept_gap = release
                    release_guard(reason, accept_gap=accept_gap)
                    break
                gap_reason = gap_reason or "diff does not cover the planned files"
                _log_guard(
                    logger,
                    "block",
                    iterations,
                    gap_reason,
                    implement_status=implement.status,
                    base=base_sha[:12],
                )
                logger.event("false_pass", iteration=iterations, summary=gap_reason[:500])
                if iterations == max_iterations:
                    stop_status = "false_pass"
                    tests_passed = False
                    test_output = (
                        test_output
                        + "\n\nFalse pass: "
                        + gap_reason
                        + "\nPassing tests were not counted.\n"
                    )
                    break
                before_fix = snapshot_tree(project)
                fix = _run_phase(
                    client, workspace, logger, profile,
                    with_project_context(
                        _fix_user_prompt(
                            task,
                            project,
                            test_output,
                            smoke_failed=False,
                            false_pass_reason=gap_reason,
                            cheat_sheet=cheat_sheet,
                            api_patterns=api_patterns,
                            base=base_sha,
                        ),
                        context_block,
                    ),
                    allowed=None,
                    max_turns=max_work_turns,
                    deadline=deadline,
                    phase=f"fix-{iterations}",
                    guard=work_guard,
                )
                if not note_fix_edits(before_fix):
                    consecutive_noop_fixes += 1
                # The gate already passed and the tree did not move. Another
                # test run would only start another fix round.
                if consecutive_noop_fixes >= 2:
                    valve = false_pass_release(
                        gap_reason,
                        consecutive_noop_fixes=consecutive_noop_fixes,
                        implement_status=implement.status,
                    )
                    reason, accept_gap = valve or (
                        "two consecutive fix rounds made no file changes and the gate passed; "
                        f"stopping the false-pass loop ({gap_reason})",
                        True,
                    )
                    release_guard(reason, accept_gap=accept_gap)
                    break
                if fix.status in {"empty_reply", "loop_detected"}:
                    stop_status = fix.status
                    break
                continue
            consecutive_noop_fixes = 0
            if iterations == max_iterations:
                stop_status = "iteration_budget"
                break
            if time.monotonic() >= deadline:
                stop_status = "time_budget"
                break
            before_fix = snapshot_tree(project)
            fix = _run_phase(
                client, workspace, logger, profile,
                with_project_context(
                    _fix_user_prompt(
                        task,
                        project,
                        test_output,
                        smoke_failed=tests_passed and not smoke_passed,
                        cheat_sheet=cheat_sheet,
                        api_patterns=api_patterns,
                        base=base_sha,
                    ),
                    context_block,
                ),
                allowed=None,
                max_turns=max_work_turns,
                deadline=deadline,
                phase=f"fix-{iterations}",
                guard=work_guard,
            )
            note_fix_edits(before_fix)
            if fix.status in {"time_budget", "empty_reply", "loop_detected", "exploration_capped"}:
                stop_status = fix.status
                # Still run the checks once more if the model managed to edit before stopping.
                if fix.status != "time_budget":
                    test_output, tests_passed, smoke_passed, ran_smoke = _run_iteration_checks(
                        project, test_cmd, lint_cmd, task, logger, iterations
                    )
                    smoke_ran = smoke_ran or ran_smoke
                    release = false_pass_release(
                        cumulative_gap(),
                        fix_produced_changes=fix_produced_changes,
                        consecutive_noop_fixes=consecutive_noop_fixes,
                        implement_status=implement.status,
                    )
                    if tests_passed and smoke_passed and release:
                        reason, accept_gap = release
                        release_guard(reason, accept_gap=accept_gap)
                break
        else:
            stop_status = "iteration_budget"

        review_text = ""
        review_passed = False
        if time.monotonic() < deadline:
            review_text, review_passed, tests_passed, smoke_passed, smoke_ran, test_output, stop_status = (
                _review_and_maybe_fix(
                    client=client,
                    workspace=workspace,
                    logger=logger,
                    profile=profile,
                    task=task,
                    context_block=context_block,
                    tests_passed=tests_passed,
                    smoke_passed=smoke_passed,
                    smoke_ran=smoke_ran,
                    test_output=test_output,
                    stop_status=stop_status,
                    deadline=deadline,
                    max_work_turns=max_work_turns,
                    test_command=test_cmd,
                    lint_command=lint_cmd,
                    test_first_files=test_first_files,
                    work_guard=work_guard,
                    snapshots=snapshots,
                    branch=branch,
                    restore_note=restore_note,
                    base=base_sha,
                )
            )
        if restore_note["ref"] and not restored_snapshot:
            restored_snapshot = restore_note["ref"]
        if snapshots and not (tests_passed and smoke_passed):
            restored = _restore_last_snapshot(project, snapshots[-1], logger)
            if restored:
                restored_snapshot = restored
                test_output, tests_passed, smoke_passed, ran_smoke = _run_iteration_checks(
                    project, test_cmd, lint_cmd, task, logger, "restore"
                )
                smoke_ran = smoke_ran or ran_smoke
                review_text = (
                    (review_text or "").rstrip()
                    + f"\n\nRestored the last passing snapshot {restored} because the run ended worse "
                    "than that tree. The snapshot ref was kept.\n"
                )
                if stop_status != "review_fix_reverted":
                    stop_status = "restored_snapshot"
        kept_failure = {
            "review_malformed",
            "review_fix_reverted",
            "restored_snapshot",
        }
        if tests_passed and smoke_passed and review_passed and stop_status not in kept_failure:
            # Re-check the cumulative diff. Review-fix can undo a tree that
            # looked real. A gap the guard already released on purpose (a fix
            # round that changed files, or two no-op rounds) stays released.
            end_gap = implementation_gap(plan_text, working_tree_diff(project, base_sha))
            if end_gap and not accept_remaining_gap:
                report_status = "false_pass"
                tests_passed = False
            else:
                report_status = "passed"
        elif stop_status in kept_failure:
            report_status = stop_status
        elif not tests_passed or not smoke_passed:
            report_status = stop_status
        else:
            report_status = "review_failed"
        return _finish(
            project, logger, task, profile, branch, log_dir,
            status=report_status,
            iterations=iterations,
            tests_passed=tests_passed,
            test_output=test_output,
            review=review_text,
            plan_status=plan.status,
            identity_note=identity_note,
            review_passed=review_passed,
            open_questions=_open_questions(plan_text, review_text),
            smoke_passed=smoke_passed,
            smoke_ran=smoke_ran,
            test_output_full=test_output,
            review_full=review_text,
            attempt=branch_attempt,
            prior_attempt=prior_attempt,
            snapshot_refs=[item.ref for item in snapshots],
            restored_snapshot=restored_snapshot,
        )
    except Exception as exc:
        logger.event("run_error", summary=f"{type(exc).__name__}: {exc}")
        if branch:
            try:
                return _finish(
                    project, logger, task, profile, branch, log_dir,
                    status="error",
                    iterations=iterations,
                    tests_passed=tests_passed,
                    test_output=test_output,
                    review=f"The run stopped with an error:\n\n{exc}",
                    plan_status=plan_status,
                    identity_note=identity_note,
                )
            except Exception:
                pass
        raise
    finally:
        logger.close()


def _review_and_maybe_fix(
    *,
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    task: TaskSpec,
    context_block: str,
    tests_passed: bool,
    smoke_passed: bool,
    smoke_ran: bool,
    test_output: str,
    stop_status: str,
    deadline: float,
    max_work_turns: int,
    test_command: str,
    lint_command: str | None,
    test_first_files: str,
    work_guard: PhaseGuard,
    snapshots: list[Snapshot],
    branch: str,
    restore_note: dict[str, str] | None = None,
    base: str = "",
) -> tuple[str, bool, bool, bool, bool, str, str]:
    """Review once. Only a well-formed review can reject the change.

    A leaked tool call or a missing section is re-prompted and, if it stays
    malformed, is not forwarded to review-fix. A real rejection gets one fix
    that is limited to the findings. If that fix breaks a passing tree, the
    last passing snapshot is restored.
    """

    review_text, problems, malformed = _one_review(
        client,
        workspace,
        logger,
        profile,
        task,
        context_block,
        tests_passed,
        deadline,
        phase="review",
        test_first_files=test_first_files,
        smoke_passed=None if not smoke_ran else smoke_passed,
        base=base,
    )
    if malformed:
        logger.event("review_unusable", summary="malformed review was not sent to review-fix")
        note = (
            review_text.rstrip()
            + "\n\nThe review was not usable (tool-call markup or a missing required section) "
            "and was not sent to review-fix.\n"
        )
        return note, False, tests_passed, smoke_passed, smoke_ran, test_output, "review_malformed"
    if not problems:
        return review_text, True, tests_passed, smoke_passed, smoke_ran, test_output, stop_status
    logger.event("review_rejected", summary="; ".join(problems)[:500], problems=problems)
    if time.monotonic() >= deadline:
        return review_text, False, tests_passed, smoke_passed, smoke_ran, test_output, stop_status
    passing_before = tests_passed and smoke_passed
    _run_phase(
        client, workspace, logger, profile,
        with_project_context(
            review_fix_prompt(task.body, shrink_text(review_text, 12_000), _status_short(workspace.root)),
            context_block,
        ),
        allowed=None,
        max_turns=max_work_turns,
        deadline=deadline,
        phase="review-fix",
        guard=work_guard,
    )
    test_output, tests_passed, smoke_passed, ran_smoke = _run_iteration_checks(
        workspace.root, test_command, lint_command, task, logger, "review-fix"
    )
    smoke_ran = smoke_ran or ran_smoke
    if passing_before and snapshots and (not tests_passed or not smoke_passed):
        restored = _restore_last_snapshot(project=workspace.root, snapshot=snapshots[-1], logger=logger)
        if restored:
            test_output, tests_passed, smoke_passed, ran_smoke = _run_iteration_checks(
                workspace.root, test_command, lint_command, task, logger, "review-fix-revert"
            )
            smoke_ran = smoke_ran or ran_smoke
            if restore_note is not None:
                restore_note["ref"] = restored
            note = (
                review_text.rstrip()
                + "\n\nreview-fix turned a passing build or test run into a failing one. "
                f"The tree was restored to {restored}. The snapshot ref was kept.\n"
            )
            return note, False, tests_passed, smoke_passed, smoke_ran, test_output, "review_fix_reverted"
    if not tests_passed or not smoke_passed:
        return review_text, False, tests_passed, smoke_passed, smoke_ran, test_output, "tests_failed"
    _record_snapshot(workspace.root, branch, snapshots, logger)
    if time.monotonic() >= deadline:
        return review_text, False, tests_passed, smoke_passed, smoke_ran, test_output, stop_status
    review_text, problems, malformed = _one_review(
        client,
        workspace,
        logger,
        profile,
        task,
        context_block,
        tests_passed,
        deadline,
        phase="review-again",
        test_first_files=test_first_files,
        smoke_passed=None if not smoke_ran else smoke_passed,
        base=base,
    )
    if malformed:
        logger.event("review_unusable", summary="second review was malformed and was not sent to another fix")
        note = (
            review_text.rstrip()
            + "\n\nThe second review was not usable and was not sent to another review-fix.\n"
        )
        return note, False, tests_passed, smoke_passed, smoke_ran, test_output, "review_malformed"
    return review_text, not problems, tests_passed, smoke_passed, smoke_ran, test_output, stop_status


def _one_review(
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    task: TaskSpec,
    context_block: str,
    tests_passed: bool,
    deadline: float,
    *,
    phase: str,
    test_first_files: str = "",
    smoke_passed: bool | None = None,
    base: str = "",
) -> tuple[str, list[str], bool]:
    """Returns review text, substantive problems, and whether the reply is unusable.

    Up to two re-prompts. A still-malformed reply is not a rejection.
    """

    require_coverage = bool(test_first_files.strip())
    reminder = ""
    text = ""
    plain_guard = PhaseGuard(expect_plain_text=True, plain_text_retries=0, exploration_cap=0)
    for attempt in range(3):
        prompt = review_prompt(
            task.body,
            fit_review_diff(working_tree_diff(workspace.root, base), REVIEW_DIFF_CHARS),
            tests_passed,
            acceptance=extract_section(task.body, "Acceptance criteria"),
            out_of_scope=extract_section(task.body, "Out of scope"),
            context_block=context_block,
            test_first_files=test_first_files,
        )
        if reminder:
            prompt += "\n\n" + reminder
        review = _run_phase(
            client, workspace, logger, profile,
            prompt,
            allowed=set(),
            max_turns=2,
            deadline=deadline,
            phase=phase if attempt == 0 else f"{phase}-reprompt-{attempt}",
            guard=plain_guard,
        )
        text = strip_leaked_tool_markup(review.content or "").strip()
        reasons = review_form_problems(text, require_test_coverage=require_coverage)
        if not reasons:
            if smoke_passed is False and "smoke" not in text.lower():
                text = text + "\n\nSmoke: the smoke command did not pass.\n"
            return text, review_problems(text, require_test_coverage=require_coverage), False
        if attempt == 2:
            break
        logger.event(
            "review_reprompt",
            phase=phase,
            attempt=attempt + 1,
            reasons=reasons,
            summary="; ".join(reasons)[:300],
        )
        reminder = review_format_reminder()
    return text, [], True


def _run_phase(
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    prompt: str,
    *,
    allowed: set[str] | None,
    max_turns: int,
    deadline: float,
    phase: str,
    guard: PhaseGuard | None = None,
) -> PhaseResult:
    logger.event("phase_start", phase=phase, max_turns=max_turns, summary=phase)
    harness = AgentHarness(
        client, workspace, logger, profile, allowed_tools=allowed
    )
    result = harness.run(prompt, max_turns=max_turns, deadline=deadline, guard=guard)
    logger.event("phase_end", phase=phase, status=result.status, summary=f"{phase} {result.status}")
    return result


def _log_guard(logger: RunLogger, action: str, iteration: Any, reason: str, **extra: Any) -> None:
    """One events.jsonl row each time the false-pass guard blocks or releases."""

    text = (reason or "").strip()
    logger.event(
        "false_pass_guard",
        action=action,
        iteration=iteration,
        reason=text[:500],
        summary=f"false_pass_guard {action}: {text[:300]}",
        **extra,
    )


def _record_snapshot(project: Path, branch: str, snapshots: list[Snapshot], logger: RunLogger) -> None:
    try:
        snap = create_snapshot(project, branch, len(snapshots) + 1)
    except Exception as exc:
        logger.event("snapshot_failed", summary=f"{type(exc).__name__}: {exc}"[:500])
        return
    snapshots.append(snap)
    logger.event(
        "snapshot",
        ref=snap.ref,
        commit=snap.sha,
        iteration=snap.number,
        summary=f"snapshot {snap.ref}",
    )


def _restore_last_snapshot(project: Path, snapshot: Snapshot, logger: RunLogger) -> str:
    try:
        restore_snapshot(project, snapshot.sha)
    except Exception as exc:
        logger.event("snapshot_restore_failed", ref=snapshot.ref, summary=f"{type(exc).__name__}: {exc}"[:500])
        return ""
    logger.event(
        "snapshot_restore",
        ref=snapshot.ref,
        commit=snapshot.sha,
        summary=f"restored {snapshot.ref}",
    )
    return snapshot.ref


def _run_checks(project: Path, test_command: str, lint_command: str | None) -> tuple[str, bool]:
    clear_bytecode(project)
    test = run_command(test_command, cwd=project, timeout=600)
    parts = [f"$ {test_command}\n{test.output}"]
    ok = test.ok
    if ok and lint_command:
        lint = run_command(lint_command, cwd=project, timeout=600)
        parts.append(f"$ {lint_command}\n{lint.output}")
        ok = lint.ok
    return "\n\n".join(parts), ok


def _run_iteration_checks(
    project: Path,
    test_command: str,
    lint_command: str | None,
    task: TaskSpec,
    logger: RunLogger,
    iteration: Any,
) -> tuple[str, bool, bool, bool]:
    """Run tests, then the smoke command when the tests passed.

    Returns output, tests_passed, smoke_passed, smoke_ran. smoke_passed stays
    true when the task has no smoke command. A smoke failure does not flip
    tests_passed; the caller treats either failure as a failed iteration.
    """

    output, tests_ok = _run_checks(project, test_command, lint_command)
    if task.must_exist:
        missing = missing_must_exist(project, task.must_exist, output)
        logger.event(
            "must_exist",
            iteration=iteration,
            missing=missing,
            passed=not missing,
            summary=f"must_exist missing={len(missing)}",
        )
        if missing:
            tests_ok = False
            listed = "\n".join(f"- {item}" for item in missing)
            output += (
                "\n\nmust_exist check failed. These paths do not exist and these names "
                f"did not appear in the test output:\n{listed}\n"
            )
    logger.event(
        "tests",
        iteration=iteration,
        passed=tests_ok,
        output=output,
        summary=f"iteration {iteration} passed={tests_ok}",
    )
    if not tests_ok or not task.smoke_command:
        return output, tests_ok, True, False
    smoke = run_smoke(
        task.smoke_command,
        cwd=project,
        mode=task.smoke_mode,
        timeout=task.smoke_timeout,
        launch_seconds=task.smoke_launch_seconds,
        fail_on_traceback=task.smoke_fail_on_traceback,
    )
    logger.event(
        "smoke",
        iteration=iteration,
        passed=smoke.ok,
        reason=smoke.reason,
        output=smoke.output,
        summary=f"smoke {smoke.reason}",
    )
    if smoke.ok:
        return output + "\n\nSmoke check passed.\n" + smoke.output, True, True, True
    failed = output + "\n\nSmoke check failed:\n" + smoke.output
    return failed, True, False, True


def _fix_user_prompt(
    task: TaskSpec,
    project: Path,
    test_output: str,
    *,
    smoke_failed: bool,
    false_pass_reason: str = "",
    cheat_sheet: str = "",
    api_patterns: tuple[str, ...] = (),
    base: str = "",
) -> str:
    diff = fit_review_diff(working_tree_diff(project, base), FIX_DIFF_CHARS)
    cited = format_traceback_files(project, test_output)
    return fix_prompt(
        task.body,
        shrink_text(test_output, 12_000),
        _status_short(project),
        smoke_failed=smoke_failed,
        diff_text=diff,
        traceback_files=cited,
        false_pass_reason=false_pass_reason,
        cheat_sheet=cheat_sheet,
        api_reminder=looks_like_api_misuse(test_output, api_patterns),
    )


def _run_test_first(
    client: ChatClient,
    workspace: Workspace,
    logger: RunLogger,
    profile: Profile,
    task: TaskSpec,
    project: Path,
    context_block: str,
    deadline: float,
    max_turns: int,
    test_command: str,
    guard: PhaseGuard | None = None,
) -> str:
    """Write tests, confirm they fail for a missing implementation, then lock them.

    Returns a short list of locked files for the review prompt, or "" when
    nothing was locked. An early pass is a warning, not a stop. Tests that
    still do not collect after one repair are not locked, so the fix loop
    can repair them.
    """

    before = snapshot_tree(project)
    _run_phase(
        client, workspace, logger, profile,
        with_project_context(
            test_first_prompt(task.body, extract_section(task.body, "Acceptance criteria")),
            context_block,
        ),
        allowed={"read", "write", "edit"},
        max_turns=max_turns,
        deadline=deadline,
        phase="test-first",
        guard=guard,
    )
    _revert_non_tests(project, before, logger)
    test_rels = _new_test_files(project, before)
    output, passed = _run_checks(project, test_command, None)
    kind = classify_test_run(output, passed)
    logger.event("test_first", classification=kind, files=test_rels, summary=kind, output=output)
    if kind == "broken" and time.monotonic() < deadline:
        _run_phase(
            client, workspace, logger, profile,
            with_project_context(
                test_first_repair_prompt(task.body, shrink_text(output, 8_000)),
                context_block,
            ),
            allowed={"read", "write", "edit"},
            max_turns=max_turns,
            deadline=deadline,
            phase="test-first-repair",
            guard=guard,
        )
        _revert_non_tests(project, before, logger)
        test_rels = _new_test_files(project, before)
        output, passed = _run_checks(project, test_command, None)
        kind = classify_test_run(output, passed)
        logger.event(
            "test_first",
            classification=kind,
            files=test_rels,
            summary=f"after repair {kind}",
            output=output,
        )
    if kind == "passed_early":
        message = (
            "warning: test-first tests passed before the implementation. "
            "Continuing. They are locked so later phases cannot weaken them."
        )
        logger.event("test_first_warning", summary=message)
        print(message, file=sys.stderr)
    if kind == "broken" or not test_rels:
        logger.event(
            "test_first_warning",
            summary="not locking test files" if kind == "broken" else "no test files changed",
        )
        return ""
    locked = workspace.lock_files(test_rels)
    logger.event("test_first_lock", paths=locked, summary=f"locked {len(locked)} file(s)")
    lines = []
    for rel in locked:
        digest = file_digest(workspace.locked[rel])
        lines.append(f"- {rel} sha256={digest[:12]}")
    return "\n".join(lines)


def _revert_non_tests(project: Path, before: dict[str, bytes], logger: RunLogger) -> None:
    after = snapshot_tree(project)
    non_tests = [rel for rel in changed_rels(before, after) if not is_test_path(rel)]
    restored = restore_rels(project, before, non_tests)
    if restored:
        logger.event("test_first_restore", paths=restored, summary="reverted non-test edits")


def _new_test_files(project: Path, before: dict[str, bytes]) -> list[str]:
    after = snapshot_tree(project)
    rels = []
    for rel in changed_rels(before, after):
        if is_test_path(rel) and (project / rel).is_file():
            rels.append(rel)
    return rels


def _finish(
    project: Path,
    logger: RunLogger,
    task: TaskSpec,
    profile: Profile,
    branch: str,
    log_dir: Path,
    *,
    status: str,
    iterations: int,
    tests_passed: bool,
    test_output: str,
    review: str,
    plan_status: str,
    identity_note: str = "",
    review_passed: bool = True,
    open_questions: str = "",
    smoke_passed: bool = True,
    smoke_ran: bool = False,
    test_output_full: str = "",
    review_full: str = "",
    attempt: int = 0,
    prior_attempt: str = "",
    snapshot_refs: list[str] | None = None,
    restored_snapshot: str = "",
) -> OvernightResult:
    refs = list(snapshot_refs or [])
    report = _render_report(
        task=task,
        profile_name=profile.name,
        branch=branch,
        status=status,
        iterations=iterations,
        tests_passed=tests_passed,
        test_output=test_output,
        review=review,
        plan_status=plan_status,
        log_dir=log_dir,
        identity_note=identity_note,
        open_questions=open_questions,
        review_passed=review_passed,
        smoke_passed=smoke_passed,
        smoke_ran=smoke_ran,
        attempt=attempt,
        prior_attempt=prior_attempt,
        snapshot_refs=refs,
        restored_snapshot=restored_snapshot,
    )
    exclude_agent_dir(project)
    report_path = _write_agent_file(project, REPORT_FILE, report)
    commit = _commit(project, task.title, status)
    logger.event("run_end", status=status, commit=commit, summary=f"{status} commit={commit}")
    stored_output = test_output_full or test_output
    stored_review = review_full or review
    return OvernightResult(
        status=status,
        branch=branch,
        report_path=report_path,
        commit=commit,
        iterations=iterations,
        tests_passed=tests_passed,
        log_dir=log_dir,
        review_passed=review_passed,
        smoke_passed=smoke_passed,
        smoke_ran=smoke_ran,
        test_output=stored_output,
        review=stored_review,
        attempts=attempt or 1,
        snapshot_refs=refs,
        restored_snapshot=restored_snapshot,
    )


def _render_report(
    *,
    task: TaskSpec,
    profile_name: str,
    branch: str,
    status: str,
    iterations: int,
    tests_passed: bool,
    test_output: str,
    review: str,
    plan_status: str,
    log_dir: Path,
    identity_note: str = "",
    open_questions: str = "",
    review_passed: bool = True,
    smoke_passed: bool = True,
    smoke_ran: bool = False,
    attempt: int = 0,
    prior_attempt: str = "",
    snapshot_refs: list[str] | None = None,
    restored_snapshot: str = "",
) -> str:
    note = f"\n{identity_note}\n" if identity_note else ""
    questions = open_questions.strip() or "(none recorded)"
    if not smoke_ran:
        smoke_line = "- Smoke: not run\n"
    else:
        smoke_line = f"- Smoke passed: {'yes' if smoke_passed else 'no'}\n"
    attempt_line = f"- Attempt: {attempt}\n" if attempt else ""
    earlier = ""
    if prior_attempt.strip():
        earlier = f"## Earlier attempt\n\n{shrink_text(prior_attempt.strip(), 2000)}\n\n"
    snap_line = ""
    if snapshot_refs:
        listed = ", ".join(snapshot_refs)
        snap_line = f"- Passing snapshots: {listed}\n"
    if restored_snapshot:
        snap_line += f"- Restored passing snapshot: {restored_snapshot}\n"
    return (
        f"# Agent report\n\n"
        f"- Status: {status}\n"
        f"- Tests passed: {'yes' if tests_passed else 'no'}\n"
        f"- Review passed: {'yes' if review_passed else 'no'}\n"
        f"{smoke_line}"
        f"- Profile: {profile_name}\n"
        f"- Task: {task.title}\n"
        f"- Branch: {branch}\n"
        f"{attempt_line}"
        f"- Test iterations: {iterations}\n"
        f"- Plan phase: {plan_status}\n"
        f"{snap_line}"
        f"- Log: {log_dir}\n"
        f"{note}\n"
        f"{earlier}"
        f"## Review\n\n{review.strip() or '(no review)'}\n\n"
        f"## Open questions\n\n{questions}\n\n"
        f"## Last test output\n\n```\n{shrink_text(test_output or '(tests did not run)', 8000)}\n```\n"
    )


def _plan_document(task: TaskSpec, plan: str) -> str:
    return f"# Plan: {task.title}\n\n{plan.strip()}\n"


def agent_dir(project: Path) -> Path:
    return project / AGENT_DIR_NAME


def exclude_agent_dir(project: Path) -> None:
    """Keep `.agent/` out of commits via the repo-local exclude file.

    `.git/info/exclude` is not a file in the user's tree, so this does not
    edit `.gitignore` or any other committed file.
    """

    exclude = project / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    lines = [line.strip() for line in existing.splitlines()]
    if ".agent/" in lines or ".agent" in lines:
        return
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    exclude.write_text(existing + suffix + ".agent/\n", encoding="utf-8")


def _write_agent_file(project: Path, name: str, text: str) -> Path:
    exclude_agent_dir(project)
    directory = agent_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _open_questions(*texts: str) -> str:
    chunks = []
    for text in texts:
        section = extract_section(text, "Open questions")
        if section.strip():
            chunks.append(section.strip())
    return "\n\n".join(chunks)


def fit_review_diff(diff_text: str, limit: int) -> str:
    """Keep the file list, then as much patch as fits, and name what was cut.

    A silent character trim used to drop the tail of the diff with no mention
    of the files the model never saw.
    """

    if len(diff_text) <= limit:
        return diff_text
    marker = "\n# Patch\n"
    if marker in diff_text:
        stat, patch = diff_text.split(marker, 1)
        stat = stat + marker
    else:
        stat, patch = "", diff_text
    note_room = 400
    budget = max(0, limit - len(stat) - note_room)
    included, omitted = _take_diff_files(patch, budget)
    note = ""
    if omitted:
        listed = "\n".join(f"- {name}" for name in omitted)
        note = (
            "\n\nFiles omitted from the patch (they are still in the file list above; "
            f"read them in the repo):\n{listed}\n"
        )
    # Keep the omission note even when the patch budget was optimistic.
    # Never cut the file list to make the note fit. A slightly long prompt is
    # better than a review that cannot see which files changed.
    return stat + included + note


def _take_diff_files(patch: str, budget: int) -> tuple[str, list[str]]:
    if not patch.strip():
        return "", []
    parts = re.split(r"(?=^diff --git )", patch, flags=re.M)
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for part in parts:
        if not part.strip():
            continue
        name = _diff_file_name(part) or part.splitlines()[0][:120]
        if used + len(part) <= budget:
            included.append(part)
            used += len(part)
        else:
            omitted.append(name)
    return "".join(included), omitted


def _diff_file_name(chunk: str) -> str:
    match = re.search(r"^diff --git a/.+ b/(.+)$", chunk, flags=re.M)
    if match:
        return match.group(1).strip()
    plus = re.search(r"^\+\+\+ b/(.+)$", chunk, flags=re.M)
    return plus.group(1).strip() if plus else ""


def ensure_repository(project: Path, *, init_repo: bool) -> str | None:
    """Make sure `project` is a git repo.

    With `init_repo`, a folder that is not a repository gets `git init` and an
    empty initial commit. Returns a note when git has no user.name or
    user.email, so the caller can show it. The note is None when identity is
    already configured. Global git config is never written.
    """

    if not (project / ".git").exists():
        if not init_repo:
            raise RuntimeError(
                f"{project} is not a git repository. "
                "Pass --init-repo to `python -m local_agent overnight`, "
                "or -InitRepo to scripts/overnight.ps1, "
                "to create the repository and an empty initial commit."
            )
        project.mkdir(parents=True, exist_ok=True)
        _git(project, "init", "-b", "main")
        _commit_with_identity(project, "commit", "--allow-empty", "-m", "Initial commit")
    return missing_identity_message(project)


def missing_identity_message(project: Path) -> str | None:
    missing = [key for key in ("user.name", "user.email") if not _config_get(project, key)]
    if not missing:
        return None
    listed = " and ".join(missing)
    return (
        f"Git {listed} is not set. "
        "This run will commit as Local Coding Agent <local-agent@localhost> "
        "without changing your git config. "
        'To use your own name: git config --global user.name "Your Name" '
        'and git config --global user.email "you@example.com".'
    )


def _require_clean_git(project: Path) -> None:
    if not (project / ".git").exists():
        raise RuntimeError(
            f"{project} is not a git repository. "
            "Pass --init-repo to `python -m local_agent overnight`, "
            "or -InitRepo to scripts/overnight.ps1, "
            "to create the repository and an empty initial commit."
        )
    status = _git(project, "status", "--porcelain")
    if status.strip():
        raise RuntimeError(
            f"{project} has uncommitted changes. Commit or stash them before an overnight run.\n{status}"
        )


def _create_branch(project: Path, slug: str, attempt: int = 0) -> str:
    """Create the working branch.

    A single run keeps the timestamp name. A retry uses ``agent/<slug>-rN``
    so the second attempt is ``agent/<slug>-r2``. A name that already exists
    gets a timestamp suffix instead of failing the checkout.
    """

    if attempt >= 1:
        name = f"agent/{slug}-r{attempt}"
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        name = f"agent/{slug}-{stamp}"
    if _branch_exists(project, name):
        name = f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    _git(project, "checkout", "-b", name)
    return name


def _branch_exists(project: Path, name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _current_branch(project: Path) -> str:
    name = _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not name or name == "HEAD":
        raise RuntimeError(f"{project} is in detached HEAD.")
    return name


def _reset_to_branch(project: Path, branch: str) -> None:
    """Leave a failed attempt and return to the branch it started from.

    The failed commit stays on its agent branch. Uncommitted agent edits are
    dropped so the next attempt sees the same clean tree the first one did.
    Ignored files (venv, weights) are not removed.
    """

    _git(project, "reset", "--hard", check=False)
    _git(project, "clean", "-fd", "-e", ".agent", check=False)
    _git(project, "checkout", "-f", branch)


def _commit(project: Path, title: str, status: str) -> str | None:
    _git(project, "add", "-A")
    staged = _git(project, "diff", "--cached", "--name-only")
    if not staged.strip():
        return None
    message = f"agent: {title} ({status})"
    _commit_with_identity(project, "commit", "-m", message)
    return _git(project, "rev-parse", "--short", "HEAD").strip()


def _commit_with_identity(project: Path, *args: str) -> str:
    """Commit even when user.name or user.email is unset.

    The fallback is a one-shot `git -c` and does not write git config.
    A rejection still says how to set the identity, instead of only pasting
    git's "please tell me who you are" text.
    """

    try:
        return _git(project, *_git_identity_args(project), *args)
    except RuntimeError as exc:
        text = str(exc)
        lowered = text.lower()
        if "user.email" in lowered or "user.name" in lowered or "identity unknown" in lowered:
            raise RuntimeError(
                "Git could not commit because user.name or user.email is missing, "
                "and the fallback identity was not accepted.\n"
                'Set them with: git config --global user.name "Your Name"\n'
                'and git config --global user.email "you@example.com".\n'
                f"{text}"
            ) from exc
        raise


def _git_identity_args(project: Path) -> list[str]:
    args: list[str] = []
    if not _config_get(project, "user.name"):
        args.extend(["-c", "user.name=Local Coding Agent"])
    if not _config_get(project, "user.email"):
        args.extend(["-c", "user.email=local-agent@localhost"])
    return args


def _config_get(project: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "config", "--get", key],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# Cap one new file so a generated log cannot push every other file out of the
# review prompt. The whole diff is capped again before it is sent.
_NEW_FILE_CHARS = 12_000


def working_tree_diff(project: Path, base: str | None = None) -> str:
    """Full change set against `base` (or HEAD), including untracked files.

    `git diff` and `git diff HEAD` only describe tracked files, and `git diff
    HEAD` drops anything already committed on the agent branch. The false-pass
    guard passes the task's base commit so a later `git commit` does not make
    the cumulative work look empty. A run that starts from an empty commit and
    creates textstats.py leaves those files untracked until the final
    `git add`, so a review that only ran `git diff` told the model the diff
    was missing.
    """

    revision = (base or "").strip() or "HEAD"
    label = "HEAD" if revision == "HEAD" else revision[:12]
    tracked_stat = ""
    tracked_patch = ""
    if _rev_exists(project, revision):
        tracked_stat = _git(project, "diff", revision, "--stat").strip()
        tracked_patch = _git(project, "diff", revision).strip()
    untracked = [
        line.strip()
        for line in _git(project, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip()
    ]
    if not tracked_stat and not tracked_patch and not untracked:
        return f"(no changes relative to {label})\n"
    parts = [f"# Changes since {label}", ""]
    if tracked_stat:
        parts.append(tracked_stat)
    if untracked:
        parts.append("Untracked files (new):")
        parts.extend(f"  {name}" for name in untracked)
    parts.extend(["", "# Patch", ""])
    if tracked_patch:
        parts.append(tracked_patch)
        parts.append("")
    for rel in untracked:
        parts.append(_new_file_patch(project, rel).rstrip())
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def _head_exists(project: Path) -> bool:
    return _rev_exists(project, "HEAD")


def _rev_exists(project: Path, rev: str) -> bool:
    if not str(rev).strip():
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", rev],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _new_file_patch(project: Path, rel: str) -> str:
    path = project / rel
    header = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n"
    if not path.is_file():
        return header + "(not a regular file)\n"
    size = path.stat().st_size
    if size > 1_000_000:
        return header + f"(new file, {size} bytes, contents omitted)\n"
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return header + f"Binary file {rel} added ({size} bytes)\n"
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    truncated = ""
    if len(text) > _NEW_FILE_CHARS:
        text = text[:_NEW_FILE_CHARS]
        truncated = "\\ [truncated]\n"
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    if not lines:
        return header + "@@ -0,0 +0,0 @@\n(empty file)\n"
    body = "".join(f"+{line}\n" for line in lines)
    return header + f"@@ -0,0 +1,{len(lines)} @@\n" + body + truncated


def _status_short(project: Path) -> str:
    return _git(project, "status", "--porcelain") or "(clean)"


def _git(project: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout


def make_client(profile: Profile, base_url: str | None = None) -> ChatClient:
    url = base_url or f"http://{profile.host}:{profile.port}"
    return ChatClient(url, profile)


def load_profile(name: str) -> Profile:
    return get_profile(name)


def failure_summary(result: OvernightResult) -> str:
    """Short note injected into the next attempt. Tests stay the judge."""

    parts = [
        f"Status: {result.status}",
        f"Branch: {result.branch}",
        f"Tests passed: {'yes' if result.tests_passed else 'no'}",
        f"Smoke passed: {'yes' if result.smoke_passed else 'no'}",
        f"Review passed: {'yes' if result.review_passed else 'no'}",
        f"Iterations: {result.iterations}",
    ]
    questions = extract_section(result.review or "", "Open questions")
    if questions.strip():
        parts.append("Open questions:\n" + questions.strip())
    if (result.review or "").strip():
        parts.append("Review:\n" + shrink_text(result.review, 1500))
    if (result.test_output or "").strip():
        parts.append("Failing checks:\n" + shrink_text(result.test_output, 2000))
    return shrink_text("\n\n".join(parts), 4000)


def _passed(result: OvernightResult) -> bool:
    return bool(result.tests_passed and result.review_passed and result.smoke_passed)


def run_with_retries(
    *,
    retries: int = 0,
    stop_file: Path | None = None,
    **kwargs: Any,
) -> OvernightResult:
    """Run a task, and on a retryable failure start again from the original branch.

    `retries` is the number of extra attempts. Zero keeps today's single run,
    including leaving the worktree on the agent branch. A stop file is checked
    only between attempts.
    """

    from local_agent.control import stop_is_requested

    retries = max(0, int(retries))
    project = Path(kwargs["project"]).resolve()
    kwargs["project"] = project
    base = _current_branch(project)
    prior = ""
    history: list[dict[str, Any]] = []
    last: OvernightResult | None = None
    for attempt in range(1, retries + 2):
        if attempt > 1 and stop_file is not None and stop_is_requested(Path(stop_file)):
            if last is not None:
                last.stop_requested = True
                last.attempts = attempt - 1
                last.attempt_history = history
            break
        if attempt > 1:
            _reset_to_branch(project, base)
        call = dict(kwargs)
        call["prior_attempt"] = prior
        call["branch_attempt"] = attempt if retries else 0
        if retries:
            call["log_dir"] = Path(kwargs["log_dir"]) / f"attempt-{attempt}"
        last = run_overnight(**call)
        history.append(
            {
                "attempt": attempt,
                "status": last.status,
                "branch": last.branch,
                "commit": last.commit,
                "tests_passed": last.tests_passed,
                "review_passed": last.review_passed,
                "smoke_passed": last.smoke_passed,
            }
        )
        last.attempts = attempt
        last.attempt_history = list(history)
        if _passed(last):
            return last
        if last.status not in RETRYABLE_STATUSES or attempt == retries + 1:
            break
        prior = failure_summary(last)
    assert last is not None
    if retries and not _passed(last):
        _reset_to_branch(project, base)
    return last
