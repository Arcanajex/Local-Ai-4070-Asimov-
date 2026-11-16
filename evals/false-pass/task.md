---
test_command: python -m pytest -q
must_exist:
  - feature.py
---

# Add feature.answer

## Goal

Create `feature.py` with `answer()` returning 42. The existing test file passes without that module, so a green pytest run is not enough on its own.

## Acceptance criteria

- [ ] `feature.py` exists
- [ ] `answer()` returns 42
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not delete `test_keep.py`.
- Do not add a command-line interface.
