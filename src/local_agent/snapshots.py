"""Side-ref snapshots of a passing worktree.

A passing test iteration is recorded as a commit on
``refs/asimov/snapshots/<branch>/<n>`` without moving HEAD. The refs stay
after the run so a later review-fix can be thrown away and the passing tree
can still be checked out by hand.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Snapshot:
    ref: str
    sha: str
    number: int


def snapshot_ref_name(branch: str, number: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "task"
    return f"refs/asimov/snapshots/{slug}/{number}"


def create_snapshot(project: Path, branch: str, number: int) -> Snapshot:
    """Commit the current worktree to a side ref. HEAD and the real index stay put."""

    root = Path(project)
    ref = snapshot_ref_name(branch, number)
    index_fd, index_name = tempfile.mkstemp(prefix="asimov-index-")
    os.close(index_fd)
    # An empty file is not a valid index. Git creates the file itself.
    os.unlink(index_name)
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_name
        name, email = _identity(root)
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = name
        env["GIT_COMMITTER_EMAIL"] = email
        if _head_exists(root):
            _git(root, "read-tree", "HEAD", env=env)
        _git(root, "add", "-A", env=env)
        tree = _git(root, "write-tree", env=env).strip()
        args = ["commit-tree", tree, "-m", f"asimov snapshot {number}"]
        if _head_exists(root):
            parent = _git(root, "rev-parse", "HEAD").strip()
            args.extend(["-p", parent])
        sha = _git(root, *args, env=env).strip()
        _git(root, "update-ref", ref, sha)
    finally:
        for suffix in ("", ".lock"):
            try:
                os.unlink(index_name + suffix)
            except OSError:
                pass
    return Snapshot(ref=ref, sha=sha, number=number)


def restore_snapshot(project: Path, sha: str) -> None:
    """Make the worktree and index match a snapshot commit. HEAD is not moved.

    Untracked files that are not in the snapshot are removed. Ignored files
    (a venv, ``.agent/``) are left alone.
    """

    root = Path(project)
    _git(root, "checkout", sha, "--", ".")
    _git(root, "clean", "-fd", "-e", ".agent")


def _identity(project: Path) -> tuple[str, str]:
    name = _config(project, "user.name") or "Local Coding Agent"
    email = _config(project, "user.email") or "local-agent@localhost"
    return name, email


def _config(project: Path, key: str) -> str:
    result = subprocess.run(
        ["git", "config", "--get", key],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _head_exists(project: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _git(project: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout
