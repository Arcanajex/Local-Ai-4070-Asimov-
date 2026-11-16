"""Hidden check: the shell test passes and test.sh was not edited.

The script is POSIX sh. Windows has no ``sh`` on PATH. Git Bash counts when
it is installed, because ``test.sh`` calls ``sh greet.sh``. When neither is
available the checker exits with the skip status instead of raising
FileNotFoundError, and the scoreboard does not count that as a failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _helpers():
    try:
        from local_agent.shells import (
            SHELL_SKIP_CODE,
            SHELL_SKIP_STATUS,
            find_posix_shell,
            posix_shell_env,
        )
    except ImportError:
        src = Path(__file__).resolve().parents[2] / "src"
        sys.path.insert(0, str(src))
        from local_agent.shells import (
            SHELL_SKIP_CODE,
            SHELL_SKIP_STATUS,
            find_posix_shell,
            posix_shell_env,
        )
    return SHELL_SKIP_CODE, SHELL_SKIP_STATUS, find_posix_shell, posix_shell_env


def main() -> int:
    skip_code, skip_status, find_posix_shell, posix_shell_env = _helpers()
    project = Path(sys.argv[1]).resolve()
    starter = Path(__file__).resolve().parent / "starter" / "test.sh"
    current = project / "test.sh"
    if not current.is_file() or current.read_bytes() != starter.read_bytes():
        print("test.sh changed or is missing")
        return 1
    shell = find_posix_shell()
    if not shell:
        print(skip_status)
        return skip_code
    # Git for Windows checks the fixture out with CRLF when core.autocrlf is
    # on. Git Bash's igncr option treats those scripts as POSIX text. Linux
    # sh does not have that option.
    argv = [shell, "test.sh"]
    if sys.platform == "win32":
        argv = [shell, "-o", "igncr", "test.sh"]
    try:
        proc = subprocess.run(
            argv,
            cwd=project,
            env=posix_shell_env(shell),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        print(skip_status)
        return skip_code
    if proc.returncode != 0:
        print(proc.stdout)
        print(f"test.sh exited {proc.returncode}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
