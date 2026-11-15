---
test_command: python -m pytest -q
---

# Take the first n items

## Goal

`first_n(items, n)` must return the first `n` items, starting at the beginning. It currently skips the first item.

## Acceptance criteria

- [ ] `first_n(["a", "b", "c"], 2)` returns `["a", "b"]`
- [ ] `first_n(["a"], 5)` returns `["a"]`
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not change `test_slice.py`.
