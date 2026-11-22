"""Command strings for the shell the harness actually starts.

The overnight runner and the smoke gate pass `test_command` and
`smoke_command` to one platform shell: bash on Linux, Windows PowerShell
5.1 on Windows (`powershell.exe -Command`). Those two shells do not accept
the same text.

PowerShell treats a quoted token as a string expression. ``"C:\\...\\python.exe"
-m pytest`` is a ParserError (`UnexpectedToken`) until the call operator is
in front: ``& 'C:\\...\\python.exe' -m pytest``. Quoting uses single quotes so
the value is literal. PowerShell 5.1's `-Command` parser strips unescaped
double quotes out of the native command line; single quotes survive that pass.

``true`` and ``false`` are bash builtins. PowerShell 5.1 has neither, so a
task whose test command is ``true`` never records a passing run.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path


SHELL_SKIP_STATUS = "skipped: needs sh"
SHELL_SKIP_CODE = 86


def quote_for_shell(token: str) -> str:
    """Quote one argument for bash, or for Windows PowerShell."""

    if os.name == "nt":
        return "'" + token.replace("'", "''") + "'"
    return shlex.quote(token)


def format_shell_command(argv: tuple[str, ...] | list[str]) -> str:
    """Join an argument vector into one platform-shell command.

    On Windows the first token is invoked with ``&``, which is required once
    the program path is quoted. Paths with spaces (a venv under a user
    profile, ``Program Files``) are the usual case.
    """

    if not argv:
        raise ValueError("argv must not be empty")
    quoted = " ".join(quote_for_shell(part) for part in argv)
    if os.name == "nt":
        return f"& {quoted}"
    return quoted


def python_shell(*args: str) -> str:
    """Run this interpreter in the platform shell.

    Tests and the mock smoke build commands from ``sys.executable`` so they
    hit the same venv that launched them. A bare quoted path is not a valid
    PowerShell command.
    """

    return format_shell_command((sys.executable, *args))


def command_succeeds() -> str:
    """A command that exits 0 in bash and in Windows PowerShell."""

    return "exit 0"


def command_fails() -> str:
    """A command that exits 1 in bash and in Windows PowerShell."""

    return "exit 1"


_AND_AND = re.compile(r"\s*&&\s*")


def split_shell_chain(command: str) -> list[str]:
    """Split a `&&` chain into separate commands.

    Windows PowerShell 5.1 has no `&&` operator. Each step is run on its
    own, and a failing step skips the rest of that chain.
    """

    text = (command or "").strip()
    if not text:
        return []
    return [part.strip() for part in _AND_AND.split(text) if part.strip()]


def adapt_platform_command(command: str) -> str:
    """Return one command the platform shell can execute.

    On Windows, a quoted program path is a string expression until the call
    operator is in front: ``& 'C:\\path\\python.exe' args``. A command that
    already starts with ``&`` is left alone. Split `&&` before calling this.
    """

    text = (command or "").strip()
    if not text or os.name != "nt" or text.startswith("&"):
        return text
    if text[0] in {"'", '"'}:
        return f"& {text}"
    return text


def platform_shell_commands(command: str) -> list[str]:
    """Commands to run, in order, on bash or Windows PowerShell 5.1."""

    return [adapt_platform_command(part) for part in split_shell_chain(command)]


# A shell that rejects the command (parse error, command not found) returns
# before any test output. A real suite that happens to mention these phrases
# is much longer than a diagnostic, so the short-output check stays narrow.
_INVALID_COMMAND_SECONDS = 20
_INVALID_OUTPUT = (
    "parsererror",
    "commandnotfoundexception",
    "is not recognized as the name of a cmdlet",
    "is not a valid statement separator",
    "unexpectedtoken",
    "not recognized as an internal or external command",
    "syntax error near unexpected token",
    "syntax error: unexpected end of file",
)


@dataclass
class ShellChain:
    ok: bool
    output: str
    invalid: bool
    command: str


def split_control_chain(command: str) -> list[tuple[str, str]]:
    """Split on `&&` and `||` that sit outside quotes.

    The first operator is "". A single `|` stays in the command: PowerShell
    5.1 can pipe, and `dotnet test --filter Name~A|B` is not a chain.
    `&&` / `||` inside quotes are not operators.
    """

    text = (command or "").strip()
    if not text:
        return []
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    operator = ""
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            buf.append(char)
            if char == quote:
                # PowerShell escapes a single quote by doubling it.
                if quote == "'" and index + 1 < len(text) and text[index + 1] == "'":
                    buf.append("'")
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            segment = "".join(buf).strip()
            if segment:
                parts.append((operator, segment))
            operator = text[index : index + 2]
            buf = []
            index += 2
            continue
        buf.append(char)
        index += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append((operator, tail))
    elif not parts:
        return []
    return parts


def shell_output_is_invalid(output: str) -> bool:
    """True when the shell rejected the command instead of running a program.

    PowerShell 5.1 parse errors and command-not-found, and bash's
    command-not-found (exit 127) or syntax error. A test process that exits
    non-zero does not match. A timed-out run is a real failure.
    """

    text = output or ""
    if re.search(r"(?m)^timed_out:\s*true\b", text):
        return False
    if re.search(r"(?m)^exit_code:\s*127\s*$", text):
        return True
    body = text.split("--- output ---", 1)[-1]
    lowered = body.lower()
    if any(needle in lowered for needle in _INVALID_OUTPUT):
        return True
    if "command not found" in lowered and len(body) < 800:
        return True
    return False


def run_shell_chain(command: str, *, cwd: Path, timeout: int) -> ShellChain:
    """Run a verify command, including `&&` / `||`, on bash and PowerShell 5.1.

    PowerShell 5.1 has neither operator. Each segment is its own process, in
    the project root. A failed segment skips the next `&&` command. A
    successful segment skips the next `||` command. The same split is used
    on bash: `exit` inside `bash -lc` would otherwise leave the shell before
    the next operator runs, and separate processes keep both CI jobs on the
    same rules.
    A segment the shell rejects is `invalid` so the caller can fall back to
    the task test command instead of counting a step failure.
    """

    from local_agent.tools import run_command

    text = (command or "").strip()
    segments = split_control_chain(text)
    if not segments:
        return ShellChain(ok=False, output="", invalid=True, command=text)
    blocks: list[str] = []
    ok = False
    invalid = False
    previous: bool | None = None
    for operator, segment in segments:
        if previous is not None and not _chain_should_run(operator, previous):
            continue
        adapted = adapt_platform_command(segment)
        started = time.monotonic()
        ran = run_command(adapted, cwd=cwd, timeout=timeout)
        elapsed = time.monotonic() - started
        blocks.append(f"$ {adapted}\n{ran.output}")
        ok = ran.ok
        previous = ok
        if not ok and elapsed <= _INVALID_COMMAND_SECONDS and shell_output_is_invalid(ran.output):
            invalid = True
            break
    return ShellChain(ok=ok, output="\n\n".join(blocks), invalid=invalid, command=text)


def _chain_should_run(operator: str, previous_ok: bool) -> bool:
    """Left-associative `&&` / `||`, matching bash once `exit` is not inlined."""

    if operator == "&&":
        return previous_ok
    if operator == "||":
        return not previous_ok
    return True


def find_posix_shell() -> str | None:
    """Return ``sh`` or ``bash``, including Git Bash when it is not on PATH.

    Windows PowerShell cannot run the shell-script eval. Git for Windows
    ships bash at ``Git\\bin\\bash.exe``, next to ``git.exe`` but not on PATH
    (``git`` itself lives in ``Git\\cmd``).
    """

    for name in ("sh", "bash"):
        found = shutil.which(name)
        if found:
            return found
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents:
            if parent.name.lower() == "git":
                candidates.append(parent / "bin" / "bash.exe")
                candidates.append(parent / "bin" / "sh.exe")
                candidates.append(parent / "usr" / "bin" / "bash.exe")
                candidates.append(parent / "usr" / "bin" / "sh.exe")
                break
    roots: list[Path] = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    roots.extend(
        (
            Path(r"C:\Program Files"),
            Path(r"C:\Program Files (x86)"),
        )
    )
    for root in roots:
        candidates.append(root / "Git" / "bin" / "bash.exe")
        candidates.append(root / "Git" / "bin" / "sh.exe")
        candidates.append(root / "Git" / "usr" / "bin" / "bash.exe")
        candidates.append(root / "Programs" / "Git" / "bin" / "bash.exe")
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return str(candidate)
    return None


def posix_shell_env(shell: str) -> dict[str, str]:
    """Environment for running a script that itself calls ``sh``.

    Git Bash's ``bash.exe`` does not always put ``usr\\bin`` (where ``sh.exe``
    lives) on PATH. The shell-script fixture runs ``sh greet.sh`` from inside
    ``test.sh``, so that directory has to be visible.
    """

    env = os.environ.copy()
    shell_dir = Path(shell).resolve().parent
    extras = [str(shell_dir)]
    usr_bin = shell_dir.parent / "usr" / "bin"
    if usr_bin.is_dir():
        extras.append(str(usr_bin))
    mingw = shell_dir.parent / "mingw64" / "bin"
    if mingw.is_dir():
        extras.append(str(mingw))
    env["PATH"] = os.pathsep.join([*extras, env.get("PATH", "")])
    return env
