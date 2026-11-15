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
