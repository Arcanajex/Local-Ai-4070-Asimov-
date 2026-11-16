# Task template

The planning bot writes one file per step. The local model reads that file
as the spec. Copy this shape. `python -m local_agent validate path/to/task.md`
checks it before an overnight run, and the queue checks every file in the
folder before the first task starts.

```markdown
---
test_command: python -m pytest -q
lint_command: python -m ruff check .
smoke_command: python app.py
smoke_mode: launch
smoke_launch_seconds: 6
context:
  - src/app.py
  - docs/decisions.md
must_exist:
  - src/app.py
  - test_widget
verify_commands:
  - python app.py --frames 60
verify_timeout: 120
extract_verify_commands: false
plan_turns: 8
implement_turns: 30
exploration_cap: 10
---

# Short title

## Goal

What should be true when this task is done, in a few sentences.

## Relevant files

- `src/app.py` — the module this step changes
- `tests/test_app.py` — the test that proves it

## Conventions and decisions

Decisions the model must not reopen. Point at `AGENTS.md` in the target repo
for rules that apply to every task. Repeat a decision here only when this
step depends on it.

## Acceptance criteria

- [ ] `widget()` returns the new status string
- [ ] `python -m pytest -q` passes
- [ ] No files outside `src/app.py` and `tests/test_app.py` are edited

## Out of scope

- Do not change the notification format. That is the next task.
- Do not add dependencies.

## Open questions policy

If a requirement is ambiguous, do not guess. End the plan and the final
reply with an `Open questions` heading and list what is unresolved. The
runner copies that heading into `.agent/AGENT_REPORT.md`. A guess that
contradicts `AGENTS.md` or this file fails the self-review.
```

## Front matter

| Key | Required | Meaning |
| --- | --- | --- |
| `test_command` | Yes, unless the target repo is Python | Command the runner executes. Python repos that omit it use `python -m pytest -q`. |
| `lint_command` | No | Runs only after the tests pass. |
| `smoke_command` | No | Runs after the tests pass. A nonzero exit or a Python traceback fails the iteration and is pasted into the fix prompt. |
| `smoke_mode` | No | `run` (default) waits for the command. `launch` is for a GUI or a server: start it, wait `smoke_launch_seconds` (default 6), treat still-running with no traceback as a pass, then kill the process tree. An exit during that wait is a crash. |
| `smoke_timeout` | No | Seconds for `smoke_mode: run`. Default 120, maximum 600. |
| `smoke_launch_seconds` | No | Seconds to wait in `launch` mode. Default 6. Clamped to 1–60. |
| `smoke_fail_on_traceback` | No | Default true. Set `false` to ignore a traceback in the output. |
| `test_first` | No | `true` writes tests from the acceptance criteria before implementation and locks those files. Default off. The CLI flags are `--test-first` and `--no-test-first` (`-TestFirst`, `-NoTestFirst`). |
| `context` | No | Repo-relative files inlined into the Project context block, after `AGENTS.md`. A YAML list or a comma-separated line. |
| `must_exist` | No | Paths that must exist on disk, or names that must appear in the test output, before a green test command counts. A list or a comma-separated line. Unset means no extra check. |
| `plan_turns` | No | Turn budget for the plan phase. Default 8. A profile override or `runtime\overrides.json` key of the same name is used when the task omits it. The task wins over the profile. A caller that passes a non-default plan budget (the eval runner does) wins over the task. |
| `implement_turns` | No | Turn budget for implement, fix, and review-fix. Default 30. Same precedence as `plan_turns`. |
| `exploration_cap` | No | Consecutive read-only or probing tool calls without a file write before the harness says to stop exploring and write. Default 10. `0` disables the cap. |
| `exploration_bonus_turns` | No | Extra turns, once, when a phase hits its budget by reading and probing instead of editing. Default 0. |
| `cheat_sheet_marker` | No | Extra heading substring, matched case-insensitively, whose section is injected verbatim into implement, fix, and review-fix. The default marker is `cheat sheet`. |
| `blocked_probes` | No | Extra regular expressions refused as shell commands, added to the built-in probe list (ilspycmd, `dotnet new`, reflection loads, and similar). |
| `api_error_patterns` | No | Extra substrings that mark a build or compile failure as API misuse. The defaults are `CS1503`, `CS0214`, `CS1061`, and `CS0117`. A match re-injects the cheat sheet into the fix prompt. |
| `scratch_dirs` | No | Extra directory names the model may not create unless the plan names that path. Defaults include `diag`, `apicheck`, `api-check`, `_probe`, `scratch`, and `throwaway`. |
| `verify_commands` | No | Commands the runner executes after tests and smoke pass, before review. Each command's exit code and an output tail are included in the harness checks the reviewer sees. A nonzero exit or a timeout fails the iteration and is pasted into the fix prompt, the same way a smoke failure is. A YAML list or a comma-separated line. On Windows the shell is PowerShell 5.1: `&&` is split into separate steps, and a command that starts with a quoted path is invoked with `&`. |
| `verify_timeout` | No | Seconds for each verify command. Default 120, maximum 600. |
| `extract_verify_commands` | No | Default `false`. When `true`, backtick-quoted run commands in Acceptance criteria are added to `verify_commands`. A flag fragment such as `` `--frames 60` ``, a function call, or a bare filename is not extracted. Leave this off unless the criteria name real commands. |
| `review_verify_only_pass` | No | Default `true`. A review that votes `accept` or `fix` (or reject / needs changes) is accepted when every open item only asks to run, verify, check, or confirm, and the harness build, tests, `must_exist`, smoke, and verify commands all passed. An acceptance line marked `not met` or `unverified` counts as verify-only when its only stated reason is that the harness did not run or execute the check. The runner logs `review_verify_only_pass` with those items. A concrete defect, including an ambiguous `not met` line, still starts review-fix. A claim that a file or symbol is missing is dropped when the tree contains it. Set `false` to keep every such vote on the review-fix path. |

A task file may start with a UTF-8 BOM. It is stripped before front matter is read. If the file starts with `---` and has no closing `---`, `validate` reports that the front matter could not be parsed instead of a missing `test_command`.

`plan_turns`, `implement_turns`, `exploration_cap`, `exploration_bonus_turns`, `cheat_sheet_marker`, `review_verify_only_pass`, `verify_commands`, `verify_timeout`, and `extract_verify_commands` may also sit in the target repo's `AGENTS.md` front matter. A key set on the task wins, then `AGENTS.md`, then the profile. `review_verify_only_pass` and `extract_verify_commands` may also be set on the profile in `profiles/models.json` or `runtime/overrides.json`. `extract_verify_commands` defaults to false.

The same smoke keys can sit in the front matter of the target repo's `AGENTS.md`. A key set on the task wins. `validate` prints a warning, and still exits 0, when a task mentions a GUI toolkit or a server and neither the task nor `AGENTS.md` sets `smoke_command`. Other tasks are not warned about.

A task or queue run can also pass `--retries N` (`-Retries`). The queue default is 1. A single overnight run defaults to 0. A retry starts a clean branch `agent/<slug>-r2` from the original base after the iteration budget, a detected loop, or a failed review. The stop file is checked between attempts. The task file is archived only when an attempt passes.

## Required headings

`Goal`, `Acceptance criteria`, and `Out of scope`. The queue refuses to start
if any task file in the folder is missing one of them. Pass `--no-strict`
(`-NoStrict` in the PowerShell scripts) for an old file that predates this
template.

`AGENTS.md` at the root of the target repo is always included when it exists.
It is not named in `context`.

## After the task passes

A queue moves a passing file into `tasks\archive` in this repo and keeps the
folders under the tasks root. `tasks\foo.md` becomes `tasks\archive\foo.md`.
`tasks\ui\step-2.md` becomes `tasks\archive\ui\step-2.md`. A name that is
already there gets a timestamp suffix; the older file is not overwritten.

A failing file stays in the task folder so it can be edited and run again.
`queue-summary.md` lists the files that were archived and the files that
stayed. `-NoArchive` (`--no-archive`) leaves the older `done\` and `failed\`
layout in place. `python -m local_agent archive path\to\task.md` moves one
file the same way, and a passing `overnight` run does too unless `-NoArchive`
is set.

The queue does not read `tasks\archive`, including when that folder is inside
the directory you passed as `-TaskDir`.
