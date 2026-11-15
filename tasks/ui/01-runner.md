---
test_command: python -m pytest -q
---

# Asimov UI step 1: task files and launching runs

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

## This step

Create `asimov_ui/runner.py with:

- `DEFAULT_AGENT_ROOT = Path(r"C:\Users\Arcanajex\Documents\Local-Ai-4070-Asimov-")`
- `slugify(text: str) -> str`: lowercase, non-alphanumeric runs become single hyphens, stripped of leading/trailing hyphens, max 40 chars, `"task"` if empty.
- `build_task_markdown(title: str, directive: str, test_command: str = "python -m pytest -q") -> str`: returns the task file text in the exact format above. Raise `ValueError` if title or directive is empty/whitespace.
- `write_task_file(tasks_dir: Path, title: str, directive: str, test_command: str = "python -m pytest -q", now: datetime | None = None) -> Path`: creates `tasks_dir` if needed and writes `<YYYYmmdd-HHMMSS>-<slug>.md` (UTF-8), returning the path.
- `build_command(agent_root: Path, task_path: Path, project_path: Path, profile: str = "quality", init_repo: bool = True) -> list[str]`: the argument list for the PowerShell command above. Raise `ValueError` if profile is not `"quality"` or `"day"`.
- `start_run(command: list[str], log_path: Path) -> subprocess.Popen`: creates the log's parent folder, opens the log file, and starts the process with stdout and stderr both redirected to it (`stderr=subprocess.STDOUT`), using `creationflags=subprocess.CREATE_NO_WINDOW` when that attribute exists.

Create `tests/test_runner.py` covering slugify edge cases, the exact markdown output, empty-input errors, file naming with a fixed `now`, the command list contents (with and without `-InitRepo`), invalid profile, and `start_run` with `subprocess.Popen` mocked.
