---
test_command: python -m pytest -q
---

# Usage must exit 2

## Goal

`greet.py` prints a usage line and exits 0 when the name is missing. Missing arguments must exit 2. A name must print `hello NAME` and exit 0.

## Acceptance criteria

- [ ] `main([])` returns 2 and the usage text mentions `NAME`
- [ ] `main(["Ada"])` returns 0 and prints `hello Ada`
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not add a dependency.
- Do not change `test_greet.py`.
