"""Hidden check: the new module exists and counts words. The test file is unchanged."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    starter_test = Path(__file__).resolve().parent / "starter" / "test_textstats.py"
    current = project / "test_textstats.py"
    if not current.is_file():
        print("test_textstats.py is missing")
        return 1
    if hashlib.sha256(current.read_bytes()).hexdigest() != hashlib.sha256(starter_test.read_bytes()).hexdigest():
        print("test_textstats.py was modified")
        return 1
    if not (project / "textstats.py").is_file():
        print("textstats.py was not created")
        return 1
    sys.path.insert(0, str(project))
    from textstats import count_words

    if count_words("a b") != 2 or count_words("") != 0:
        print("count_words returned the wrong value")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
