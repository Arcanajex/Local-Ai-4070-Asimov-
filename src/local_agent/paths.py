"""Locate the repo, the runtime directory, and profile files.

The runtime directory holds llama.cpp, GGUF weights, the venv, and logs.
It is gitignored. Override it with LOCAL_AGENT_RUNTIME when the weights live
on another drive.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Directory that contains profiles/models.json.

    Resolved from this file (src/local_agent/paths.py) so the package still
    works from a source checkout before it is installed.
    """

    here = Path(__file__).resolve()
    candidate = here.parents[2]
    if (candidate / "profiles" / "models.json").is_file():
        return candidate
    # Editable installs keep the files in the original tree; walk upward
    # from the current working directory as a fallback.
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        if (parent / "profiles" / "models.json").is_file():
            return parent
    return candidate


def runtime_dir() -> Path:
    override = os.environ.get("LOCAL_AGENT_RUNTIME")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "runtime"


def profiles_path() -> Path:
    override = os.environ.get("LOCAL_AGENT_PROFILES")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "profiles" / "models.json"


def overrides_path() -> Path:
    override = os.environ.get("LOCAL_AGENT_OVERRIDES")
    if override:
        return Path(override).expanduser().resolve()
    return runtime_dir() / "overrides.json"


def models_dir() -> Path:
    return runtime_dir() / "models"


def llama_dir() -> Path:
    return runtime_dir() / "llama.cpp"


def llama_server_path() -> Path:
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    return llama_dir() / name
