---
test_command: python -m pytest -q
---

# Default the flag to on

## Goal

`enabled(value)` returns whether a feature is on. When `value` is omitted or `None`, the default is on. An explicit false stays off.

## Acceptance criteria

- [ ] `enabled(None)` returns `True`
- [ ] `enabled(False)` returns `False`
- [ ] `enabled(True)` returns `True`
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not read environment variables.
- Do not change `test_settings.py`.
