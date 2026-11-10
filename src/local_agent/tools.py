"""The four tools local models actually complete: read, write, edit, shell.

Schemas stay flat on purpose. Nested JSON and large optional objects are
where  the 8GB bench saw tool-call failures. The shell always has a timeout
because a blocking command (a server, `tail -f`, a prompt) otherwise hangs
the whole agent turn.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_READ_LINES = 400
MAX_WRITE_BYTES = 1_000_000
DEFAULT_SHELL_TIMEOUT = 120
MAX_SHELL_TIMEOUT = 600
SHELL_OUTPUT_CHARS = 16_000

# Tripwires for an unattended run. This is not a sandbox: the model can run
# what the user can run. These patterns stop the obvious machine-killers
# without blocking `Remove-Item` of a build directory inside the project.
_BLOCKED_COMMANDS = (
    re.compile(r"\bgit\s+push\b", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\b", re.IGNORECASE),
    re.compile(r"\b(shutdown|restart-computer|stop-computer)\b", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\bclear-disk\b", re.IGNORECASE),
    re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),
    # `rm -rf /` and the same with the flags in either order. Paths under the
    # project (`rm -rf ./build`) are allowed.
    re.compile(r"(^|[;&|]\s*)rm\s+-[a-zA-Z]*\s+/(?:\s|$)", re.IGNORECASE),
)


@dataclass
class ToolResult:
    ok: bool
    output: str


def shell_description() -> str:
    if os.name == "nt":
        return (
            "Run one PowerShell command in the project root. "
            "The process is killed after timeout_seconds (default 120, max 600). "
            "Do not start a server or anything else you need to keep alive."
        )
    return (
        "Run one bash command in the project root. "
        "The process is killed after timeout_seconds (default 120, max 600). "
        "Do not start a server or anything else you need to keep alive."
    )


def tool_schemas(allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """OpenAI tool definitions. `allowed` restricts a phase (plan is read-only)."""

    specs = {
        "read": {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a UTF-8 text file. Paths are relative to the project root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "description": "1-based start line. Default 1."},
                        "limit": {"type": "integer", "description": "Maximum lines. Default 400."},
                    },
                    "required": ["path"],
                },
            },
        },
        "write": {
            "type": "function",
            "function": {
                "name": "write",
                "description": "Create or overwrite a UTF-8 text file inside the project.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "edit": {
            "type": "function",
            "function": {
                "name": "edit",
                "description": (
                    "Replace one exact occurrence of old_string with new_string. "
                    "Read the file first. old_string must match once, including whitespace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        "shell": {
            "type": "function",
            "function": {
                "name": "shell",
                "description": shell_description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["command"],
                },
            },
        },
    }
    names = ["read", "write", "edit", "shell"]
    if allowed is not None:
        names = [name for name in names if name in allowed]
    return [specs[name] for name in names]


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"project root does not exist: {self.root}")

    def resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(raw_path)
        full = candidate if candidate.is_absolute() else (self.root / candidate)
        # Resolve lexically first so `..` cannot escape before we compare.
        full = Path(os.path.abspath(full))
        try:
            full.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes the project root: {raw_path}") from exc
        return full

    def read(self, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> ToolResult:
        try:
            target = self.resolve(path)
        except ValueError as exc:
            return ToolResult(False, str(exc))
        if not target.is_file():
            return ToolResult(False, f"file not found: {path}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(False, f"file is not UTF-8 text: {path}")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        start = max(1, int(offset or 1))
        count = MAX_READ_LINES if limit is None else max(1, min(int(limit), MAX_READ_LINES))
        selected = lines[start - 1 : start - 1 + count]
        numbered = [f"{start + index}|{line}" for index, line in enumerate(selected)]
        header = f"{path}: lines {start}-{start + len(selected) - 1} of {len(lines)}"
        body = "\n".join(numbered)
        if start - 1 + count < len(lines):
            body += "\n[file continues; read again with a higher offset]"
        return ToolResult(True, header + "\n" + body)

    def write(self, path: str, content: str) -> ToolResult:
        if not isinstance(content, str):
            return ToolResult(False, "content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            return ToolResult(False, f"refusing to write {len(encoded)} bytes (limit {MAX_WRITE_BYTES})")
        try:
            target = self.resolve(path)
        except ValueError as exc:
            return ToolResult(False, str(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        text = content.replace("\r\n", "\n").replace("\r", "\n")
        target.write_text(text, encoding="utf-8", newline="\n")
        # A .pyc written earlier in the same wall-clock second stays "fresh"
        # (Python stores mtimes as whole seconds) and pytest will keep running
        # the old bytecode. Drop it whenever the source changes.
        invalidate_bytecode(target)
        return ToolResult(True, f"wrote {path} ({len(text.encode('utf-8'))} bytes)")

    def edit(self, path: str, old_string: str, new_string: str) -> ToolResult:
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return ToolResult(False, "old_string and new_string must be strings")
        if old_string == "":
            return ToolResult(False, "old_string must not be empty")
        if old_string == new_string:
            return ToolResult(False, "old_string and new_string are identical")
        try:
            target = self.resolve(path)
        except ValueError as exc:
            return ToolResult(False, str(exc))
        if not target.is_file():
            return ToolResult(False, f"file not found: {path}")
        try:
            original = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(False, f"file is not UTF-8 text: {path}")
        text = original.replace("\r\n", "\n").replace("\r", "\n")
        old_norm = old_string.replace("\r\n", "\n").replace("\r", "\n")
        new_norm = new_string.replace("\r\n", "\n").replace("\r", "\n")
        count = text.count(old_norm)
        if count == 0:
            return ToolResult(False, "old_string was not found. Read the file and copy the exact text.")
        if count > 1:
            return ToolResult(
                False,
                f"old_string matched {count} times. Include more surrounding lines so it matches once.",
            )
        updated = text.replace(old_norm, new_norm, 1)
        target.write_text(updated, encoding="utf-8", newline="\n")
        invalidate_bytecode(target)
        return ToolResult(True, f"edited {path}")

    def shell(self, command: str, timeout_seconds: int | None = None) -> ToolResult:
        if not isinstance(command, str) or not command.strip():
            return ToolResult(False, "command must be a non-empty string")
        blocked = blocked_command_reason(command)
        if blocked:
            return ToolResult(False, blocked)
        timeout = _clamp_timeout(timeout_seconds)
        return run_command(command, cwd=self.root, timeout=timeout)


def invalidate_bytecode(path: Path) -> None:
    """Delete cached bytecode for one source file."""

    if path.suffix != ".py":
        return
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for cached in cache.glob(path.stem + "*.pyc"):
        cached.unlink(missing_ok=True)


def clear_bytecode(root: Path) -> None:
    """Delete project bytecode so a test run cannot replay a stale .pyc."""

    for cache in root.rglob("__pycache__"):
        if not cache.is_dir() or ".git" in cache.parts:
            continue
        for cached in cache.glob("*.pyc"):
            cached.unlink(missing_ok=True)


def blocked_command_reason(command: str) -> str | None:
    for pattern in _BLOCKED_COMMANDS:
        if pattern.search(command):
            return (
                "command blocked by the overnight tripwire "
                f"({pattern.pattern}). Do not push, hard-reset, clean the tree, "
                "or shut the machine down. Change the project with read/write/edit instead."
            )
    return None


def _clamp_timeout(timeout_seconds: int | None) -> int:
    if timeout_seconds is None:
        return DEFAULT_SHELL_TIMEOUT
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_SHELL_TIMEOUT
    return max(1, min(value, MAX_SHELL_TIMEOUT))


def run_command(command: str, *, cwd: Path, timeout: int) -> ToolResult:
    if os.name == "nt":
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
        popen_kwargs: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        argv = ["/bin/bash", "-lc", command]
        popen_kwargs = {"start_new_session": True}
    env = os.environ.copy()
    # Don't write .pyc from agent commands. Combined with invalidate_bytecode,
    # a test rerun in the same second cannot keep executing the previous file.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            **popen_kwargs,
        )
    except FileNotFoundError as exc:
        return ToolResult(False, f"failed to start shell: {exc}")
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
        stdout, stderr = proc.communicate()
    output = _format_shell_output(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        timed_out=timed_out,
        stdout=stdout or "",
        stderr=stderr or "",
        timeout=timeout,
    )
    return ToolResult(not timed_out and proc.returncode == 0, output)


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, 15)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()


def _format_shell_output(
    *,
    exit_code: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    timeout: int,
) -> str:
    combined = stdout
    if stderr:
        combined = combined + ("\n" if combined else "") + stderr
    combined = _cap_output(combined, SHELL_OUTPUT_CHARS)
    status = "timed_out: true" if timed_out else "timed_out: false"
    note = f"\nkilled after {timeout}s" if timed_out else ""
    return f"exit_code: {exit_code}\n{status}{note}\n--- output ---\n{combined}"


def _cap_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = max(0, limit - head - 80)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[truncated {omitted} chars]...\n{text[-tail:] if tail else ''}"


def dispatch(name: str, arguments: dict[str, Any], workspace: Workspace) -> ToolResult:
    arguments = arguments or {}
    try:
        if name == "read":
            return workspace.read(
                arguments.get("path", ""),
                offset=int(arguments.get("offset") or 1),
                limit=int(arguments.get("limit") or MAX_READ_LINES),
            )
        if name == "write":
            return workspace.write(arguments.get("path", ""), arguments.get("content", ""))
        if name == "edit":
            return workspace.edit(
                arguments.get("path", ""),
                arguments.get("old_string", ""),
                arguments.get("new_string", ""),
            )
        if name == "shell":
            timeout = arguments.get("timeout_seconds")
            return workspace.shell(arguments.get("command", ""), timeout_seconds=timeout)
        return ToolResult(False, f"unknown tool: {name}")
    except (TypeError, ValueError) as exc:
        return ToolResult(False, f"bad arguments for {name}: {exc}")
