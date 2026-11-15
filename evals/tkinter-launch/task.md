---
test_command: python -m pytest -q
smoke_command: python app.py
smoke_mode: launch
smoke_launch_seconds: 6
---

# Launch the item window

## Goal

`app.py` is a small tkinter window that remembers an output path. It must start and stay open. It must not crash, and it must not mix `pack` and `grid` on the same widget.

## Acceptance criteria

- [ ] `output_file(Path("data"), "items.txt")` returns `Path("data") / "items.txt"`
- [ ] `build` uses only one geometry manager. Do not call both `pack` and `grid`.
- [ ] `python app.py` stays running (a window) and does not print a traceback
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not add a dependency.
- Do not change `test_app.py` except to fix an import path if the module moves. The assertions stay.
