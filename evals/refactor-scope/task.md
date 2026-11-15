---
test_command: python -m pytest -q
---

# Collapse blank lines in normalize

## Goal

`calc.normalize` should collapse runs of whitespace to a single space and strip the ends. `notes.py` is a different feature and must stay byte-for-byte identical.

## Acceptance criteria

- [ ] `normalize("  a   b  ")` returns `"a b"`
- [ ] `python -m pytest -q` passes
- [ ] `notes.py` is unchanged

## Out of scope

- Do not edit `notes.py`.
- Do not change `test_calc.py`.
