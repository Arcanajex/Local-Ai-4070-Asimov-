"""File tools, path jail, shell timeout, and the unattended-run tripwire."""

from __future__ import annotations

import sys

from local_agent.tools import Workspace, blocked_command_reason, tool_schemas


def test_write_read_edit_round_trip(tmp_path):
    workspace = Workspace(tmp_path)
    written = workspace.write("src/app.py", "def value():\r\n    return 1\r\n")
    assert written.ok
    text = (tmp_path / "src" / "app.py").read_bytes()
    assert b"\r" not in text
    read = workspace.read("src/app.py")
    assert read.ok
    assert "return 1" in read.output
    edited = workspace.edit("src/app.py", "return 1", "return 2")
    assert edited.ok
    assert "return 2" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")


def test_edit_drops_bytecode_from_the_same_second(tmp_path):
    import py_compile

    workspace = Workspace(tmp_path)
    workspace.write("adder.py", "def add(a, b):\n    return a - b\n")
    py_compile.compile(tmp_path / "adder.py", doraise=True)
    cache = list((tmp_path / "__pycache__").glob("adder*.pyc"))
    assert cache
    edited = workspace.edit("adder.py", "return a - b", "return a + b")
    assert edited.ok
    assert list((tmp_path / "__pycache__").glob("adder*.pyc")) == []
    assert "return a + b" in (tmp_path / "adder.py").read_text(encoding="utf-8")


def test_edit_rejects_missing_and_ambiguous_matches(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.write("a.txt", "same\nsame\n")
    missing = workspace.edit("a.txt", "nope", "x")
    assert not missing.ok
    ambiguous = workspace.edit("a.txt", "same", "other")
    assert not ambiguous.ok
    assert "2 times" in ambiguous.output
    empty = workspace.edit("a.txt", "", "x")
    assert not empty.ok


def test_paths_cannot_escape_the_project(tmp_path):
    workspace = Workspace(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    for raw in ("../outside.txt", str(outside), "nested/../../outside.txt"):
        result = workspace.write(raw, "nope")
        assert not result.ok, raw
        assert not outside.exists()


def test_shell_timeout_kills_a_blocking_command(tmp_path):
    workspace = Workspace(tmp_path)
    command = f"{sys.executable} -c \"import time; time.sleep(30)\""
    result = workspace.shell(command, timeout_seconds=1)
    assert not result.ok
    assert "timed_out: true" in result.output
    assert "killed after 1s" in result.output


def test_shell_returns_output_and_caps_timeout(tmp_path):
    workspace = Workspace(tmp_path)
    result = workspace.shell("echo hello-from-shell", timeout_seconds=99999)
    assert result.ok
    assert "hello-from-shell" in result.output
    assert "timed_out: false" in result.output


def test_tripwire_blocks_push_and_shutdown_but_allows_normal_commands():
    assert blocked_command_reason("git push origin main")
    assert blocked_command_reason("git reset --hard HEAD")
    assert blocked_command_reason("shutdown /s /t 0")
    assert blocked_command_reason("rm -rf /")
    assert blocked_command_reason("echo ok") is None
    assert blocked_command_reason("rm -rf ./build") is None


def test_tool_schemas_are_flat_and_can_be_restricted_to_read():
    names = [schema["function"]["name"] for schema in tool_schemas()]
    assert names == ["read", "write", "edit", "shell"]
    for schema in tool_schemas():
        properties = schema["function"]["parameters"]["properties"]
        for prop in properties.values():
            assert prop["type"] in {"string", "integer"}
    readonly = [schema["function"]["name"] for schema in tool_schemas({"read"})]
    assert readonly == ["read"]
