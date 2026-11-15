---
test_command: python -m pytest -q
---

# Add textstats.py

## Goal

Create a new module `textstats.py` with `count_words(text)` that returns how many whitespace-separated words are in `text`. The test file already exists. Do not delete it.

## Acceptance criteria

- [ ] `textstats.py` exists and defines `count_words`
- [ ] `count_words("a b")` returns 2
- [ ] `count_words("")` returns 0
- [ ] `python -m pytest -q` passes

## Out of scope

- Do not add a command-line interface.
- Do not edit `test_textstats.py`.
