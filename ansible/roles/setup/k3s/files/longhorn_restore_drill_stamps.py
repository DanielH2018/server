#!/usr/bin/env python3
"""The restore drill's stamp files, as the Longhorn backup-plane heartbeat reads them.

The drill (longhorn-restore-drill.sh) writes these under its stamp directory for checks 7 and 8
to read: `last-success`, the `candidates` list with its per-volume `seen/` and `success/` stamps,
and `excluded_oversize`. Split out of longhorn_backup_health.py when that reader reached its
line cap; every function here takes the stamp directory rather than reading the environment.
"""

from __future__ import annotations

import os


def read_stamp(path: str) -> tuple[str | None, bool]:
    """(content, unreadable) — content's trailing newlines stripped, matching bash `$(cat ...)`.

    `unreadable` is True only when the file EXISTS but couldn't be opened (permissions, a
    directory in its place, ...) — distinct from FileNotFoundError, which is the ordinary
    "never written" case and returns `(None, False)`. Bash's own `[[ -r ]]` did NOT make this
    distinction — it folded both into one boolean, and that fold is what caused the 2026-08-19
    incident: the stamp directory's mode made a FRESH, successful drill's stamp unreadable by
    this script's user, and `[[ -r ]]` reported the same "false" it would have for a stamp that
    never existed at all — "no restore drill has ever succeeded", permanently, while a drill had
    just in fact succeeded. This function is what fixes that: it distinguishes the two cases
    bash could not. See check_restore_drill()'s docstring in the logic module for how this is
    consumed.
    """
    try:
        with open(path) as fh:
            return fh.read().rstrip("\n"), False
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True


def read_candidates(stamp_dir: str) -> list[str]:
    path = os.path.join(stamp_dir, "candidates")
    try:
        with open(path) as fh:
            return [line for line in fh.read().splitlines() if line.strip()]
    except OSError:
        return []


def read_excluded_oversize(stamp_dir: str) -> list[tuple[str, int]]:
    """The drill's `excluded_oversize` file as (name, actualSize) pairs.

    A missing file reads as empty: a drill that predates the file, or has not run yet, has
    excluded nothing it can name. A malformed line is kept with size 0 rather than dropped, so a
    format drift still names the volume.
    """
    path = os.path.join(stamp_dir, "excluded_oversize")
    try:
        with open(path) as fh:
            lines = [line for line in fh.read().splitlines() if line.strip()]
    except OSError:
        return []
    excluded = []
    for line in lines:
        name, _, size = line.partition("\t")
        excluded.append((name, int(size) if size.isdigit() else 0))
    return excluded


def read_seen(stamp_dir: str, candidates: list[str]) -> dict[str, float]:
    seen = {}
    for cand in candidates:
        try:
            seen[cand] = os.stat(os.path.join(stamp_dir, "seen", cand)).st_mtime
        except OSError:
            continue
    return seen


def read_success(stamp_dir: str, candidates: list[str]) -> dict[str, str]:
    # check 8 only needs "did this candidate ever succeed" per volume, not why one didn't — the
    # missing-vs-unreadable distinction (check 7's fix, above) doesn't carry an extra message
    # here, so the second element is discarded.
    success = {}
    for cand in candidates:
        content, _unreadable = read_stamp(os.path.join(stamp_dir, "success", cand))
        if content is not None:
            success[cand] = content
    return success
