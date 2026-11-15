"""Hidden check: first_n keeps the first item."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(project))
    from slice import first_n

    if first_n(["a", "b", "c"], 2) != ["a", "b"] or first_n(["a"], 5) != ["a"]:
        print("first_n is still wrong")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
