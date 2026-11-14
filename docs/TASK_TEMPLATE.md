# Task template

The planning bot writes one file per step. The local model reads that file
as the spec. Copy this shape. `python -m local_agent validate path/to/task.md`
checks it before an overnight run, and the queue checks every file in the
folder before the first task starts.

```markdown
---
test_command: python -m pytest -q
lint_command: python -m ruff check .
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
| `context` | No | Repo-relative files inlined into the Project context block, after `AGENTS.md`. A YAML list or a comma-separated line. |

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
