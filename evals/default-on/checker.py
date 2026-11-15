"""Hidden check: the default is on and an explicit false stays off."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(project))
    from settings import enabled

    if enabled(None) is not True or enabled() is not True:
        print("default is not on")
        return 1
    if enabled(False) is not False or enabled(True) is not True:
        print("explicit values are wrong")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
