#!/usr/bin/env python3
"""The restore drill's stamps, as the Longhorn backup-plane heartbeat and the other readers read them.

The drill (longhorn-restore-drill.sh) writes these under its stamp directory for checks 7 and 8
to read: `last-success`, the `candidates` list with its per-volume `seen/` and `success/` stamps,
and the `excluded_oversize` and `excluded_nopvc` lists. Split out of longhorn_backup_health.py when that reader reached its
line cap; every function here takes the stamp directory rather than reading the environment.

The same state also has a ConfigMap arm, `longhorn-restore-drill-stamps` in the drill's
namespace (H2.3a of docs/target-architecture/host/h2-shrink-host-plane.md). The ConfigMap holds
`last-success`, `candidates`, `excluded_oversize` and `excluded_nopvc` as the files' content,
`seen.<pvc>` as the epoch the seen marker's mtime held, and `success.<pvc>` as the epoch the
success file held. `select_stamps` picks the arm once per run: the ConfigMap while it holds
`last-success`, the host directory otherwise. Checks 7 and 8,
runbook_gates_lib/longhorn_upgrade.py and scripts/docs/reference/state.py all read through it,
so the four readers cannot disagree on which store is current.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field

CONFIGMAP = "longhorn-restore-drill-stamps"
# `--ignore-not-found` turns an absent ConfigMap into rc 0 with empty stdout, so every non-zero
# exit is a read that failed. Without it, NotFound would share rc 1 with a Forbidden or a timeout,
# and telling them apart would mean matching kubectl's stderr wording.
CONFIGMAP_GET_ARGS = ("get", "configmap", CONFIGMAP, "--ignore-not-found", "-o", "json")
LAST_SUCCESS = "last-success"


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
            lines = _lines(fh.read())
    except OSError:
        return []
    return _parse_oversize(lines)


def read_excluded_nopvc(stamp_dir: str) -> list[str]:
    """The drill's `excluded_nopvc` file: backed-up volume names with no bound PVC.

    A missing file reads as empty, for the same reason as `read_excluded_oversize`.
    """
    try:
        with open(os.path.join(stamp_dir, "excluded_nopvc")) as fh:
            return [line for line in fh.read().splitlines() if line.strip()]
    except OSError:
        return []


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


def _lines(text: str | None) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


def _parse_oversize(lines: list[str]) -> list[tuple[str, int]]:
    excluded = []
    for line in lines:
        name, _, size = line.partition("\t")
        excluded.append((name, int(size) if size.isdigit() else 0))
    return excluded


@dataclass(frozen=True)
class DrillStamps:
    """Everything checks 7 and 8 read, from one store.

    `where` names the store in a message. `seen` holds only the entries the store carries;
    `seen_default` is the time a candidate with no entry is judged from, None in the directory
    arm, where such a candidate is skipped as it always was.
    """

    where: str
    last_success: str | None
    last_success_unreadable: bool = False
    candidates: list[str] = field(default_factory=list)
    seen: dict[str, float] = field(default_factory=dict)
    seen_default: float | None = None
    success: dict[str, str] = field(default_factory=dict)
    excluded_oversize: list[tuple[str, int]] = field(default_factory=list)
    excluded_nopvc: list[str] = field(default_factory=list)


def from_directory(stamp_dir: str) -> DrillStamps:
    """The host-directory arm. H2.3f deletes it once the drill writes only the ConfigMap."""
    stamp_path = os.path.join(stamp_dir, LAST_SUCCESS)
    content, unreadable = read_stamp(stamp_path)
    candidates = read_candidates(stamp_dir)
    return DrillStamps(
        where=stamp_path,
        last_success=content,
        last_success_unreadable=unreadable,
        candidates=candidates,
        seen=read_seen(stamp_dir, candidates),
        success=read_success(stamp_dir, candidates),
        excluded_oversize=read_excluded_oversize(stamp_dir),
        excluded_nopvc=read_excluded_nopvc(stamp_dir),
    )


def parse_configmap(output: str) -> dict | None:
    """The ConfigMap `CONFIGMAP_GET_ARGS` printed, or None when it does not exist.

    Raises ValueError on a body that is not a ConfigMap, so a caller reports a truncated or
    wrong answer as a failed read rather than as an absent store.
    """
    if not output.strip():
        return None
    try:
        body = json.loads(output)
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("kind") != "ConfigMap":
        raise ValueError("not a ConfigMap: %s" % output.strip()[:160])
    return body


def _created_at(doc: dict) -> float | None:
    raw = (doc.get("metadata") or {}).get("creationTimestamp")
    try:
        # `%z` reads the API server's trailing `Z` as UTC.
        return _dt.datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return None


def from_configmap(doc: dict | None, namespace: str) -> DrillStamps | None:
    """The ConfigMap arm, or None while the readers fall back to the host directory.

    The fallback holds while the ConfigMap is absent and while it holds no `last-success`, so the
    empty ConfigMap H2.3b renders still reads the directory. A candidate with no `seen.<pvc>`
    takes the ConfigMap's creationTimestamp as its seen time: check 8 then flags it once the
    coverage window passes instead of skipping it forever.
    """
    if doc is None:
        return None
    data = doc.get("data") or {}
    if LAST_SUCCESS not in data:
        return None
    candidates = _lines(data.get("candidates"))
    seen = {}
    for cand in candidates:
        try:
            seen[cand] = float(data["seen." + cand])
        except KeyError, ValueError:
            continue
    success = {
        cand: data["success." + cand].rstrip("\n")
        for cand in candidates
        if "success." + cand in data
    }
    return DrillStamps(
        where="configmap %s/%s key %s" % (namespace, CONFIGMAP, LAST_SUCCESS),
        last_success=data[LAST_SUCCESS].rstrip("\n"),
        candidates=candidates,
        seen=seen,
        seen_default=_created_at(doc),
        success=success,
        excluded_oversize=_parse_oversize(_lines(data.get("excluded_oversize"))),
        excluded_nopvc=_lines(data.get("excluded_nopvc")),
    )


def select_stamps(doc: dict | None, namespace: str, stamp_dir: str) -> DrillStamps:
    """The one store this run reads: the ConfigMap arm when it applies, else the directory."""
    return from_configmap(doc, namespace) or from_directory(stamp_dir)
