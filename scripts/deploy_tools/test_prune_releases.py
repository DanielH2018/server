"""Tests for prune_releases.select_prunable -- the guard that keeps a live release on disk.

The failure this protects against is total and silent: prune the directory `current` points at,
every /usr/local/bin symlink dangles at once, and the health crons that would have gone red are
themselves the scripts that vanished. So every rule here is a `..._is_kept` / `..._is_pruned`
pair. A guard observed only from the passing side is indistinguishable from one that fires on
nothing, which this repo has paid for twice (volume-claim's short-circuit, image-smoke's bare-boot
rule).

Run: uv run pytest scripts/deploy_tools/test_prune_releases.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prune_releases import resolve_current, select_prunable  # noqa: E402


def _release(root, name, age_seconds):
    """A release dir whose mtime is `age_seconds` in the past.

    Explicit mtimes rather than creation order: the ordering rule is by mtime, and a test that
    relied on filesystem creation order would pass without exercising it.
    """
    d = root / name
    d.mkdir()
    stamp = 1_000_000_000 - age_seconds
    os.utime(d, (stamp, stamp))
    return d


def _fleet(tmp_path, count):
    """`count` releases, newest first in the returned list."""
    root = tmp_path / "releases"
    root.mkdir()
    return [_release(root, f"sha{i:02d}", age_seconds=i * 3600) for i in range(count)]


# ── rule 1: the current release is never prunable ────────────────────────────────────────────


def test_current_release_is_kept_even_when_oldest(tmp_path):
    """A group not deployed for months still has its scripts in use. Age must not outrank
    in-use."""
    dirs = _fleet(tmp_path, 8)
    oldest = dirs[-1]
    assert oldest not in select_prunable(dirs, current=oldest, keep=2)


def test_stale_release_that_is_not_current_is_pruned(tmp_path):
    dirs = _fleet(tmp_path, 8)
    newest, oldest = dirs[0], dirs[-1]
    assert oldest in select_prunable(dirs, current=newest, keep=2)


# ── rule 2: keep the N most recent ───────────────────────────────────────────────────────────


def test_recent_release_within_keep_is_kept(tmp_path):
    dirs = _fleet(tmp_path, 8)
    victims = select_prunable(dirs, current=dirs[0], keep=5)
    for keeper in dirs[:5]:
        assert keeper not in victims


def test_release_beyond_keep_is_pruned(tmp_path):
    dirs = _fleet(tmp_path, 8)
    victims = select_prunable(dirs, current=dirs[0], keep=5)
    assert set(victims) == set(dirs[5:])


def test_fleet_smaller_than_keep_prunes_nothing(tmp_path):
    dirs = _fleet(tmp_path, 3)
    assert select_prunable(dirs, current=dirs[0], keep=5) == []


# ── an unknown current means do nothing, not do everything ───────────────────────────────────


def test_missing_current_prunes_nothing(tmp_path):
    """A half-finished deploy has no usable pointer, and the release it is about to point at is
    on disk. Pruning there deletes the thing that is seconds from becoming current."""
    dirs = _fleet(tmp_path, 8)
    assert select_prunable(dirs, current=None, keep=1) == []


def test_known_current_still_prunes(tmp_path):
    dirs = _fleet(tmp_path, 8)
    assert select_prunable(dirs, current=dirs[0], keep=1) != []


# ── resolve_current ──────────────────────────────────────────────────────────────────────────


def test_live_pointer_resolves(tmp_path):
    target = _release(tmp_path, "sha00", 0)
    link = tmp_path / "current"
    link.symlink_to(target)
    assert resolve_current(link) == target.resolve()


def test_dangling_pointer_resolves_to_none(tmp_path):
    """A dangling pointer must read as unknown, so the caller prunes nothing -- not as 'no
    current', which would make everything prunable."""
    link = tmp_path / "current"
    link.symlink_to(tmp_path / "gone")
    assert resolve_current(link) is None


def test_absent_pointer_resolves_to_none(tmp_path):
    assert resolve_current(tmp_path / "never-created") is None


def test_pointer_to_a_file_resolves_to_none(tmp_path):
    """A release is a directory. A pointer at a regular file is a broken deploy, not a
    release."""
    f = tmp_path / "notadir"
    f.write_text("")
    link = tmp_path / "current"
    link.symlink_to(f)
    assert resolve_current(link) is None
