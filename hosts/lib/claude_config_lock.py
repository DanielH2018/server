#!/usr/bin/env python3
"""The lock between homelab-claude-config-render's publish and the sessions that read its output.

The publish dir holds `lock`, `current` (a link to the newest version directory) and
`versions/`. A session holds the lock shared from before it starts claude until it exits, so
the publish, which takes it exclusive before it swaps `current` or removes a version, never
moves a file a session runs on. The claude-code and renovate-agent components ship a copy beside
each script that imports it (a `lib:` entry). Stdlib only: each importer runs with
`uv run --no-project`.
"""

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

LOCK = "lock"


def hold_settings(path: str) -> str:
    """Hold the published file for this process's life; return `path` resolved.

    `path` is `<publish dir>/current/<name>.json`. Call it before starting claude, and pass the
    returned path to `--settings`. The shared lock stays held until the process exits.

    Raises:
        OSError: the publish dir has no lock, so nothing was ever published.
        RuntimeError: the published file is missing or not a regular file, so the session
            would start without its deny floor.
    """
    fd = os.open(
        os.path.join(os.path.dirname(os.path.dirname(path)), LOCK), os.O_RDONLY
    )
    fcntl.flock(fd, fcntl.LOCK_SH)
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        os.close(fd)
        raise RuntimeError(
            f"no published Claude Code settings at {path}; refusing to start"
        )
    return resolved


@contextmanager
def exclusive(publish_dir: Path) -> Generator[bool]:
    """Take the lock exclusive without waiting; yield whether it was taken.

    Creates `publish_dir` and its lock when they are missing, and sets them 0755 and 0644
    whatever the caller's umask, so every session user can traverse the dir and open the lock.
    """
    publish_dir.mkdir(exist_ok=True)
    publish_dir.chmod(0o755)
    lock_path = publish_dir / LOCK
    lock_path.touch()
    lock_path.chmod(0o644)
    with open(lock_path, "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
