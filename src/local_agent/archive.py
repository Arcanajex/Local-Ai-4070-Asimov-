"""Move a finished task file into the archive without overwriting.

The archive root defaults to ``tasks/archive`` in this repo. A file keeps its
path relative to the tasks root, so ``tasks/foo.md`` lands at
``tasks/archive/foo.md`` and ``tasks/ui/step-2.md`` lands at
``tasks/archive/ui/step-2.md``. A name that is already there gets a timestamp
suffix. Failed tasks are not archived.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

from local_agent.paths import repo_root


def tasks_archive_path(root: Path | None = None) -> Path:
    """``<root>/tasks/archive``. ``root`` defaults to this repo."""

    base = Path(root) if root is not None else repo_root()
    return base / "tasks" / "archive"


def default_archive_dir() -> Path:
    return tasks_archive_path()


def default_tasks_root() -> Path:
    return repo_root() / "tasks"


def resolve_tasks_root(task_dir: Path, explicit: Path | None = None) -> Path:
    """Folder that relative archive paths are measured from.

    An explicit root wins. Otherwise a directory inside this repo's ``tasks/``
    uses that ``tasks/`` folder, so a queue pointed at ``tasks/ui`` still
    archives under ``tasks/archive/ui/``. A project task folder named ``tasks``
    (or sitting under one) is used the same way. Anything else is its own root,
    and the archived name is the file name.
    """

    if explicit is not None:
        return Path(explicit).resolve()
    resolved = Path(task_dir).resolve()
    repo_tasks = default_tasks_root().resolve()
    if resolved == repo_tasks or _is_under(resolved, repo_tasks):
        return repo_tasks
    for candidate in [resolved, *resolved.parents]:
        if candidate.name.lower() == "tasks":
            return candidate
    return resolved


def relative_task_path(source: Path, tasks_root: Path) -> Path:
    """Path to preserve under the archive root."""

    source = Path(source).resolve()
    root = Path(tasks_root).resolve()
    try:
        relative = source.relative_to(root)
    except ValueError:
        relative = Path(source.name)
    if any(part.lower() == "archive" for part in relative.parts):
        raise ValueError(f"refusing to archive a file already under an archive folder: {source}")
    return relative


def unique_destination(dest: Path, *, now: datetime | None = None) -> Path:
    """Return ``dest`` or a sibling that does not exist yet.

    The suffix is a local timestamp (``-YYYYmmdd-HHMMSS``). If that name is
    taken too, nanoseconds are appended. The original file is never replaced.
    """

    if not dest.exists():
        return dest
    moment = now or datetime.now()
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    stamped = dest.with_name(f"{dest.stem}-{stamp}{dest.suffix}")
    if not stamped.exists():
        return stamped
    return dest.with_name(f"{dest.stem}-{stamp}-{time.time_ns()}{dest.suffix}")


def archive_task(
    source: Path,
    *,
    archive_dir: Path | None = None,
    tasks_root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Move ``source`` into the archive. Returns the path it was moved to."""

    file_path = Path(source).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"task file does not exist: {source}")
    root = resolve_tasks_root(file_path.parent, tasks_root)
    dest_root = Path(archive_dir).resolve() if archive_dir is not None else default_archive_dir()
    if file_path == dest_root or _is_under(file_path, dest_root):
        raise ValueError(f"task file is already in the archive: {file_path}")
    relative = relative_task_path(file_path, root)
    destination = unique_destination(dest_root / relative, now=now)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        file_path.replace(destination)
    except OSError:
        shutil.move(str(file_path), str(destination))
    return destination


def maybe_archive_task(
    path: Path | None,
    *,
    enabled: bool,
    succeeded: bool,
    archive_dir: Path | None = None,
    tasks_root: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Archive a single task after a successful run.

    A failed run, ``--no-archive``, or a missing file leaves the task where
    it is. Callers that are not a user-facing run (the mock smoke test) pass
    ``enabled=False``.
    """

    if not enabled or not succeeded or path is None:
        return None
    file_path = Path(path)
    if not file_path.is_file():
        return None
    return archive_task(
        file_path,
        archive_dir=archive_dir,
        tasks_root=tasks_root,
        now=now,
    )


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent
