---
test_command: python -m pytest -q
---

# Asimov UI step 3: the window

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

`asimov_ui/runner.py` (task files, commands, `start_run`) and `asimov_ui/status.py` (`RunTracker`, `tail`, `parse_result`) already exist. Read them first and reuse them; do not break them or their tests.

## This step

1. Create `asimov_ui/form.py` (no tkinter imports) with `validate_form(title: str, directive: str, project: str, profile: str) -> list[str]` returning human-readable error messages: empty title, empty directive, empty project path, project path that exists but is not a folder, profile not `quality`/`day`. An empty list means valid. A project folder that doesn't exist yet is allowed (it will be created).

2. Create `asimov_ui/app.py` with a tkinter `AsimovApp` class (`ttk` widgets) containing:
   - Entry for task title, a multi-line Text box for the directive, an entry plus "Browse..." button (`filedialog.askdirectory`) for the project folder, a combobox for the model profile (`quality` default, `day`), a checkbox "Create git repo if missing" (default on), and a read-only agent root entry pre-filled with `DEFAULT_AGENT_ROOT`.
   - A "Run" button: validates with `validate_form` (show errors with `messagebox.showerror`), creates the project folder if missing, writes the task file into `<agent_root>\tasks\ui-runs` with `write_task_file`, builds the command, starts it with `start_run` logging to `<agent_root>\runtime\logs\ui-<timestamp>.log`, and creates a `RunTracker`. Disable the Run button while a run is active.
   - A status label showing `tracker.summary()` and a scrolled, read-only log view, both refreshed every 2 seconds with `root.after` using `tail`. Never block the UI thread.
   - An overridable method `on_run_finished(self, tracker)` called exactly once when the run leaves the `running` state; for now it just re-enables the Run button.
   - A `main()` function that creates `tk.Tk()`, titles the window "Asimov - Local Coding Agent", and runs the main loop.

Create `tests/test_form.py` for `validate_form`. Tests must not create any tkinter window; at most, check that `asimov_ui.app` imports and exposes `AsimovApp` and `main`.
