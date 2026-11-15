---
test_command: python -m pytest -q
---

# Asimov UI step 4: finished notification and launcher

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

`asimov_ui/runner.py`, `asimov_ui/status.py`, `asimov_ui/form.py`, and `asimov_ui/app.py` (tkinter `AsimovApp` with an `on_run_finished(self, tracker)` hook) already exist. Read them first; keep all existing tests passing.

## This step

1. Create `asimov_ui/notify.py` with:
   - `build_toast_script(title: str, message: str) -> str`: a PowerShell script that shows a Windows 10/11 toast notification using the built-in `Windows.UI.Notifications` WinRT API (`ToastNotificationManager`, a `ToastText02` template, app id `"Asimov"` or the PowerShell app id). Escape the title and message safely for XML and for a PowerShell single-quoted string.
   - `notify(title: str, message: str, passed: bool) -> None`: runs the toast script via `subprocess.run(["powershell", "-NoProfile", "-Command", script], creationflags=CREATE_NO_WINDOW when available, timeout=15)`; plays `winsound.MessageBeep` (`MB_OK` if passed, `MB_ICONHAND` otherwise) when `winsound` is importable; never raises (catch and ignore errors, since a notification failing must not crash the app).
2. Update `AsimovApp.on_run_finished` in `app.py` to also call `notify` with `"Asimov run passed"` or `"Asimov run failed"` and `tracker.summary()`, and to bring the window to the front.
3. Create `asimov_ui/__main__.py` so `python -m asimov_ui` calls `app.main()`.
4. Create `Asimov.bat` in the project root that changes to its own folder (`cd /d "%~dp0"`) and runs `start "" pythonw -m asimov_ui` (falling back to `python -m asimov_ui` if `pythonw` is missing).
5. Create `README.md` explaining what Asimov UI does, how to open it (double-click `Asimov.bat`), and what each field means.

Create `tests/test_notify.py` covering XML/quote escaping in `build_toast_script` (titles with `<`, `&`, and `'`) and that `notify` swallows exceptions from a mocked `subprocess.run`.
