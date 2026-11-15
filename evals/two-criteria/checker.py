"""Hidden check: both key and value are returned. The unit test only covers key."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(project))
    from pair import parse_pair

    got = parse_pair("a=b")
    if got.get("key") != "a" or got.get("value") != "b":
        print(f"parse_pair returned {got!r}")
        return 1
    try:
        parse_pair("ab")
    except ValueError:
        print("ok")
        return 0
    print("missing separator did not raise")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
