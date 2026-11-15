"""Hidden check: notes.py is untouched and normalize collapses whitespace."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    starter_notes = Path(__file__).resolve().parent / "starter" / "notes.py"
    current = project / "notes.py"
    if not current.is_file() or current.read_bytes() != starter_notes.read_bytes():
        print("notes.py changed")
        return 1
    sys.path.insert(0, str(project))
    from calc import normalize

    if normalize("  a   b  ") != "a b":
        print(f"normalize returned {normalize('  a   b  ')!r}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
