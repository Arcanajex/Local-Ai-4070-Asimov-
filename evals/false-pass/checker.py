"""Hidden check: feature.answer exists. The pre-existing test does not cover it."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    if not (project / "feature.py").is_file():
        print("feature.py was not created")
        return 1
    sys.path.insert(0, str(project))
    from feature import answer

    if answer() != 42:
        print("answer() is wrong")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
