---
test_command: python -m pytest -q
context:
  - adder.py
---

# Smoke task: adder

## Goal

Create `adder.py` with a function `add(a, b)` that returns the sum of the two numbers.

Create `test_adder.py` that imports `add` and asserts `add(2, 3) == 5`.

Use the standard library only. Do not add dependencies. The test command is `python -m pytest -q`.

## Relevant files

- `adder.py`
- `test_adder.py`

## Conventions and decisions

Keep the function pure. No third-party packages.

## Acceptance criteria

- [ ] `add(2, 3)` returns 5
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not add a command-line interface.
- Do not handle non-numeric inputs.

## Open questions policy

If a requirement is ambiguous, do not guess. End the plan and the final reply with an Open questions heading and list what is unresolved.
