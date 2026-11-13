"""Command line entry points used by the PowerShell scripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from local_agent import __version__
from local_agent.client import server_is_healthy
from local_agent.config import get_profile, list_profiles
from local_agent.download import DownloadError, download_file
from local_agent.overnight import load_task, make_client, run_overnight
from local_agent.queue import run_queue
from local_agent.paths import runtime_dir
from local_agent.releases import resolve_windows_cuda_plan
from local_agent.server_args import args_for_profile, format_args
from local_agent.smoke import run_live_smoke, run_mock_smoke


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent", description="Local coding agent for llama-server.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    server = sub.add_parser("server-args", help="Print llama-server arguments for a profile.")
    server.add_argument("--profile", required=True)
    server.add_argument("--format", choices=("json", "lines"), default="json")
    server.add_argument("--allow-missing-model", action="store_true")
    server.add_argument("--host")
    server.add_argument("--port", type=int)
    server.set_defaults(func=_server_args)

    download = sub.add_parser("download", help="Resumable download of one URL.")
    download.add_argument("--url", required=True)
    download.add_argument("--dest", required=True, type=Path)
    download.add_argument("--expected-size", type=int)
    download.set_defaults(func=_download)

    releases = sub.add_parser("releases", help="Resolve the latest Windows CUDA llama.cpp zips.")
    releases.add_argument("--cuda", default="12.4", help="12.4, 13.4, or latest")
    releases.set_defaults(func=_releases)

    profiles = sub.add_parser("profiles", help="List profile names.")
    profiles.set_defaults(func=_profiles)

    health = sub.add_parser("health", help="Check llama-server /health.")
    health.add_argument("--base-url")
    health.add_argument("--profile", default="quality")
    health.set_defaults(func=_health)

    overnight = sub.add_parser("overnight", help="Plan, implement, test, review, and commit.")
    overnight.add_argument("--task", required=True, type=Path)
    overnight.add_argument("--project", required=True, type=Path)
    overnight.add_argument("--profile", default="quality")
    overnight.add_argument("--base-url")
    overnight.add_argument("--log-dir", type=Path)
    overnight.add_argument("--max-hours", type=float, default=8.0)
    overnight.add_argument("--max-iterations", type=int, default=6)
    overnight.add_argument("--test-command")
    overnight.add_argument("--lint-command")
    overnight.add_argument(
        "--init-repo",
        action="store_true",
        help="If the project is not a git repository, git init and create an empty initial commit.",
    )
    overnight.add_argument(
        "--no-strict",
        action="store_true",
        help="Run a task that does not match docs/TASK_TEMPLATE.md.",
    )
    overnight.add_argument("--context-char-limit", type=int, default=14_000)
    overnight.set_defaults(func=_overnight)

    queue = sub.add_parser("queue", help="Run a folder of task files, one at a time.")
    queue.add_argument("--task-dir", required=True, type=Path)
    queue.add_argument("--project", required=True, type=Path)
    queue.add_argument("--profile", default="quality")
    queue.add_argument("--base-url")
    queue.add_argument("--log-dir", type=Path)
    queue.add_argument("--max-hours-per-task", type=float, default=8.0)
    queue.add_argument("--max-iterations", type=int, default=6)
    queue.add_argument(
        "--init-repo",
        action="store_true",
        help="If the project is not a git repository, git init and create an empty initial commit.",
    )
    queue.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Run later task files even when one fails. The failed branch is not merged.",
    )
    queue.add_argument(
        "--no-strict",
        action="store_true",
        help="Run task files that do not match docs/TASK_TEMPLATE.md.",
    )
    queue.add_argument(
        "--allow-main",
        action="store_true",
        help="Allow the queue to merge into main or master. Prefer an integration branch.",
    )
    queue.set_defaults(func=_queue)

    validate = sub.add_parser("validate", help="Check a task file or a task folder against the template.")
    validate.add_argument("target", type=Path)
    validate.add_argument("--project", type=Path, help="Repo the tasks apply to. Defaults to the task's folder.")
    validate.add_argument("--no-strict", action="store_true")
    validate.set_defaults(func=_validate)

    tune = sub.add_parser("tune", help="Sweep offload values and write runtime/overrides.json.")
    tune.add_argument("--profile", required=True)
    tune.add_argument("--headroom-mib", type=int, default=1024)
    tune.add_argument("--load-timeout", type=int, default=300)
    tune.set_defaults(func=_tune)

    smoke = sub.add_parser("smoke", help="Run the built-in sample task.")
    smoke.add_argument("--mock", action="store_true", help="Scripted client, no GPU and no server.")
    smoke.add_argument("--live", action="store_true", help="Real llama-server, throwaway git repo.")
    smoke.add_argument("--profile", default="day")
    smoke.add_argument("--base-url")
    smoke.add_argument("--max-minutes", type=float, default=45.0)
    smoke.set_defaults(func=_smoke)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, KeyError, LookupError, DownloadError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _server_args(args: argparse.Namespace) -> int:
    argv = args_for_profile(
        args.profile,
        allow_missing_model=args.allow_missing_model,
        host=args.host,
        port=args.port,
    )
    print(format_args(argv, args.format))
    return 0


def _download(args: argparse.Namespace) -> int:
    path = download_file(args.url, args.dest, expected_size=args.expected_size)
    print(path)
    return 0


def _releases(args: argparse.Namespace) -> int:
    plan = resolve_windows_cuda_plan(args.cuda)
    print(json.dumps(plan.to_json()))
    return 0


def _profiles(_args: argparse.Namespace) -> int:
    for name in list_profiles():
        profile = get_profile(name)
        flag = "optional" if profile.optional else "default"
        print(f"{name}\t{flag}\t{profile.filename}")
    return 0


def _health(args: argparse.Namespace) -> int:
    if args.base_url:
        url = args.base_url
    else:
        profile = get_profile(args.profile)
        url = f"http://{profile.host}:{profile.port}"
    healthy = server_is_healthy(url)
    print("ok" if healthy else "down")
    return 0 if healthy else 1


def _overnight(args: argparse.Namespace) -> int:
    profile = get_profile(args.profile)
    task = load_task(args.task)
    log_dir = args.log_dir or (runtime_dir() / "logs" / "overnight")
    result = run_overnight(
        task=task,
        project=args.project,
        profile=profile,
        client=make_client(profile, args.base_url),
        log_dir=log_dir,
        max_iterations=args.max_iterations,
        max_seconds=args.max_hours * 3600,
        test_command=args.test_command,
        lint_command=args.lint_command,
        init_repo=args.init_repo,
        strict=not args.no_strict,
        context_char_limit=args.context_char_limit,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "branch": result.branch,
                "commit": result.commit,
                "tests_passed": result.tests_passed,
                "iterations": result.iterations,
                "report": str(result.report_path) if result.report_path else None,
                "log_dir": str(result.log_dir),
            }
        )
    )
    return 0 if result.tests_passed else 1


def _queue(args: argparse.Namespace) -> int:
    profile = get_profile(args.profile)
    log_dir = args.log_dir or (runtime_dir() / "logs" / "queue")
    result = run_queue(
        task_dir=args.task_dir,
        project=args.project,
        profile=profile,
        client=make_client(profile, args.base_url),
        log_dir=log_dir,
        max_seconds_per_task=args.max_hours_per_task * 3600,
        max_iterations=args.max_iterations,
        init_repo=args.init_repo,
        continue_on_failure=args.continue_on_failure,
        strict=not args.no_strict,
        allow_main=args.allow_main,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "base_branch": result.base_branch,
                "passed": result.passed,
                "failed": result.failed,
                "skipped": result.skipped,
                "stopped_on": result.stopped_on,
                "summary": str(result.summary_path),
                "state": str(result.state_path),
            }
        )
    )
    return 0 if result.status == "passed" else 1


def _validate(args: argparse.Namespace) -> int:
    from local_agent.taskcheck import collect_task_paths, format_problems, validate_task_files

    paths = collect_task_paths(args.target)
    project = args.project or (args.target if args.target.is_dir() else args.target.parent)
    if args.no_strict:
        print(f"ok ({len(paths)} file(s), strict checks skipped)")
        return 0
    problems = validate_task_files(paths, project)
    if problems:
        print(format_problems(problems), file=sys.stderr)
        return 1
    print(f"ok ({len(paths)} file(s))")
    return 0


def _tune(args: argparse.Namespace) -> int:
    from local_agent.tune import sweep_profile

    winner = sweep_profile(
        args.profile,
        headroom_mib=args.headroom_mib,
        load_timeout=args.load_timeout,
    )
    if winner is None:
        print("no offload value stayed inside the VRAM cap", file=sys.stderr)
        return 1
    profile = get_profile(args.profile)
    print(
        json.dumps(
            {
                "profile": args.profile,
                "flag": profile.offload_flag,
                "offload": winner.offload,
                "tokens_per_sec": winner.tokens_per_sec,
                "vram_used_mib": winner.vram_used_mib,
                "vram_total_mib": winner.vram_total_mib,
            }
        )
    )
    return 0


def _smoke(args: argparse.Namespace) -> int:
    if args.live:
        result = run_live_smoke(args.profile, args.base_url, args.max_minutes)
    else:
        result = run_mock_smoke()
    print(
        json.dumps(
            {
                "status": result.status,
                "tests_passed": result.tests_passed,
                "branch": result.branch,
                "commit": result.commit,
                "report": str(result.report_path) if result.report_path else None,
                "log_dir": str(result.log_dir),
            }
        )
    )
    return 0 if result.tests_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
