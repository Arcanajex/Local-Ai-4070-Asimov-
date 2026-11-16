"""Hidden check: label() returns ready."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(project))
    from box import label

    if label() != "ready":
        print("label() is wrong")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
