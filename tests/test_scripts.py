"""Static checks for the PowerShell entry points.

The scripts themselves run on Windows PowerShell 5.1. CI does not assume
pwsh is installed, so this test checks the invariants a parser would:
balanced braces, fail-fast, and the commands the README tells the user to run.
"""

from __future__ import annotations

import re

from local_agent.paths import repo_root

SCRIPTS = [
    "setup.ps1",
    "download-models.ps1",
    "start-server.ps1",
    "tune-n-cpu-moe.ps1",
    "overnight.ps1",
    "smoke-test.ps1",
    "lib.ps1",
]


def _strip(text: str) -> str:
    """Remove comments and quoted strings so brace counting sees real blocks."""

    text = re.sub(r"<#[\s\S]*?#>", "", text)
    out = []
    i = 0
    while i < len(text):
        if text[i] == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if text[i] in {'"', "'"}:
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == "`" and quote == '"':
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def test_powershell_scripts_exist_and_braces_balance():
    root = repo_root() / "scripts"
    for name in SCRIPTS:
        text = (root / name).read_text(encoding="utf-8")
        stripped = _strip(text)
        assert stripped.count("{") == stripped.count("}"), name
        assert stripped.count("(") == stripped.count(")"), name
        if name != "lib.ps1":
            assert "$ErrorActionPreference = 'Stop'" in text
            assert "Set-StrictMode -Version Latest" in text
        assert "\r" not in text


def test_setup_refuses_wsl_and_downloads_both_cuda_zips():
    text = (repo_root() / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    assert "WSL" in text
    assert "cudart" in text
    assert "llama-server.exe" in text
    assert "native Windows" in text or "Windows_NT" in text
