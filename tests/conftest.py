"""Keep tests away from a developer's runtime/overrides.json."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("LOCAL_AGENT_RUNTIME", str(runtime))
    monkeypatch.setenv("LOCAL_AGENT_OVERRIDES", str(runtime / "overrides.json"))
    # A queue that forgets to pass --archive-dir must not write into the repo.
    archive = tmp_path / "tasks-archive"
    import local_agent.archive as archive_module

    monkeypatch.setattr(archive_module, "default_archive_dir", lambda: archive)
    return runtime
