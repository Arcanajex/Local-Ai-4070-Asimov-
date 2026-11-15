---
test_command: python -m pytest -q
---

# Parse both sides of a pair

## Goal

`parse_pair("a=b")` must return a dict with both `key` and `value`. The existing test only checks `key`. Both fields are required.

## Acceptance criteria

- [ ] `parse_pair("a=b")["key"]` is `"a"`
- [ ] `parse_pair("a=b")["value"]` is `"b"`
- [ ] A string without `=` raises `ValueError`
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not add a file format or a CLI.
- Do not weaken `test_pair.py`.
