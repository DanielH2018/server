#!/usr/bin/env python3
"""Remove old host-script release directories, never the one in use.

WHY A SCRIPT AND NOT A JINJA EXPRESSION IN THE TASK. The rule this implements has exactly one
catastrophic failure mode: prune the directory `current` points at, and every symlink under
/usr/local/bin dangles at once. Every converted cron then fails silently -- cron reports
"No such file or directory" to a mail spool nobody reads, and the Kuma push that would have
gone red never runs, because the script that pushes it is the one that vanished. A guard
against that has to be observable from the failing side, and this repo's rule is that a new
check ships with a proof it can go red. A pure function with a test is that proof; a filter
chain inside a task is not.

WHY RELEASES ARE PER GROUP, not one tree for the host. A release is only meaningful as the unit
that gets deployed together, and on this repo that unit is the Ansible role: `--tags k3s`
deploys the k3s scripts and nothing else. One host-wide `current` would have to hold one commit
for artifacts that are deployed at different times from different trees, so it would be wrong
for every group but the last one deployed. renovate.json reaches the same conclusion for image
bumps and states it as "makes the rollback unit equal the deploy unit".

Usage:
    prune_releases.py <group-dir> --current <path> [--keep N] [--apply]

Prints what it would remove and exits 0. Without --apply it removes nothing, which is the
default deliberately: the caller that wants a mutation says so.
"""

import argparse
import shutil
import sys
from pathlib import Path

DEFAULT_KEEP = 5


def select_prunable(release_dirs, current, keep=DEFAULT_KEEP):
    """Return the release dirs safe to remove, oldest first.

    `release_dirs` is an iterable of Paths; `current` is the Path `current` resolves to, or
    None when the pointer is missing or dangling. Ordering is by directory mtime, because a
    release dir is named for a commit and commit ids do not sort chronologically.

    Two rules, and the first one outranks the second:
      1. The current release is NEVER prunable, even when it is the oldest on disk. A group
         that has not been deployed in months still has its scripts in use.
      2. Keep the `keep` most recent releases; everything older is prunable.

    When `current` is None nothing is pruned at all. A missing pointer means the group is in an
    unknown state, and removing directories is exactly the wrong move there -- it could delete
    the release a half-finished deploy is about to point at.
    """
    if current is None:
        return []
    dirs = sorted(
        (d for d in release_dirs if d.is_dir()), key=lambda d: d.stat().st_mtime
    )
    current = current.resolve()
    keepers = set(dirs[-keep:]) if keep > 0 else set()
    return [d for d in dirs if d.resolve() != current and d not in keepers]


def resolve_current(pointer):
    """The directory `pointer` resolves to, or None when it is missing or dangling."""
    p = Path(pointer)
    if not p.is_symlink() and not p.exists():
        return None
    try:
        target = p.resolve(strict=True)
    except OSError, RuntimeError:
        return None
    return target if target.is_dir() else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "group_dir", help="directory holding this group's <sha> release dirs"
    )
    ap.add_argument("--current", required=True, help="the group's `current` symlink")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument(
        "--apply", action="store_true", help="actually remove; default is a report"
    )
    ns = ap.parse_args(argv)

    group = Path(ns.group_dir)
    if not group.is_dir():
        print(f"no release directory at {group}; nothing to prune")
        return 0

    current = resolve_current(ns.current)
    if current is None:
        print(
            f"{ns.current} does not resolve to a directory; refusing to prune anything"
        )
        return 0

    victims = select_prunable(group.iterdir(), current, keep=ns.keep)
    if not victims:
        print(
            f"nothing to prune in {group} (keeping {ns.keep}, current={current.name})"
        )
        return 0

    for d in victims:
        print(f"{'removing' if ns.apply else 'would remove'} {d}")
        if ns.apply:
            shutil.rmtree(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
