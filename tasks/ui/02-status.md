---
test_command: python -m pytest -q
---

# Asimov UI step 2: run status and logs

## Project context (applies to every step)

This project is "Asimov UI", a small Windows desktop front end for a local coding agent. The agent itself lives in a separate folder, called the "agent root", default `C:\Users\Arcanajex\Documents\Local-Ai-4070-Asimov-`. A run is started with:
`powershell -NoProfile -ExecutionPolicy Bypass -File <agent_root>\scripts\overnight.ps1 -Task <task.md> -Project <project folder> -Profile <quality|day> [-InitRepo]`
When a run ends, its output contains one JSON line like `{"status": "passed", "branch": "...", "commit": "...", "tests_passed": true, "iterations": 1, "report": "...", "log_dir": "..."}` and the process exit code is 0 when tests passed.
A task file is markdown with YAML front matter, exactly like:
```
---
test_command: python -m pytest -q
---

# <title>

<directive text>
```
All code lives in the package folder `asimov_ui/` (with `__init__.py`) and tests in `tests/`. Python 3.10+, standard library only (tkinter, subprocess, json, pathlib, re, datetime, threading). No third-party dependencies besides pytest for tests. Use type hints and docstrings. Tests must never start a real PowerShell process or open a real window: use `unittest.mock` and `tmp_path`.

`asimov_ui/runner.py` already exists from step 1 (`write_task_file`, `build_command`, `start_run`). Do not break it or its tests.

## This step

Create `asimov_ui/status.py` with:

- `parse_result(log_text: str) -> dict | None`: find the last line that is a JSON object containing a `"status"` key and return it parsed; return `None` if there isn't one. Ignore lines that fail to parse.
- `tail(log_path: Path, lines: int = 200) -> str`: last `lines` lines of the file, read with `errors="replace"`; empty string if the file doesn't exist.
- A `RunTracker` class built from a `subprocess.Popen`-like object (anything with `poll()`), the `log_path`, the `title`, and a `started_at` datetime. It has:
  - `state() -> str` returning `"running"` while `poll()` is `None`, otherwise `"passed"` if the exit code is 0 and `"failed"` if not.
  - `result() -> dict | None` returning `parse_result` of the whole log once finished, `None` while running.
  - `elapsed(now: datetime | None = None) -> str` formatted `H:MM:SS`.
  - `summary() -> str`: a one-line human summary, e.g. `"Passed: <title> (branch agent/..., 2 iterations)"`, `"Failed: <title> (see log)"`, or `"Running: <title> (0:03:12)"`.

Create `tests/test_status.py` covering all of the above with fake process objects and `tmp_path` log files, including a log with junk lines, a missing log, and the running/passed/failed states.
