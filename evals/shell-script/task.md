---
test_command: sh test.sh
---

# Fix the greeting script

## Goal

`greet.sh` prints the wrong word. `test.sh` is the check. Make the script print `hello` and nothing else.

## Acceptance criteria

- [ ] `sh greet.sh` prints `hello`
- [ ] `sh test.sh` exits 0
- [ ] `test.sh` is unchanged

## Out of scope

- Do not rewrite this as Python.
- Do not add npm packages. `package.json` only marks the project type.
