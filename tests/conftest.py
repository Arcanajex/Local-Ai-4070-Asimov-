"""Keep tests away from a developer's runtime/overrides.json."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("LOCAL_AGENT_RUNTIME", str(runtime))
    monkeypatch.setenv("LOCAL_AGENT_OVERRIDES", str(runtime / "overrides.json"))
    return runtime
