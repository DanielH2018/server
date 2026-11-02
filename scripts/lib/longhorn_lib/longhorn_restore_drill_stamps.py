#!/usr/bin/env python3
"""The restore drill's stamps, as the Longhorn backup-plane heartbeat and the other readers read them.

The drill, the `longhorn-restore-drill` CronJob, writes its state to the ConfigMap
`longhorn-restore-drill-stamps` in its namespace for checks 7 and 8 to read. The ConfigMap holds
`last-success`, `candidates`, `excluded_oversize` and `excluded_nopvc` as text, `seen.<pvc>` as
the epoch the drill first saw a candidate, and `success.<pvc>` as the epoch of that candidate's
last passing drill. Checks 7 and 8, runbook_gates_lib/longhorn_upgrade.py and
scripts/docs/reference/state.py all read through `from_configmap`, so the four readers cannot
disagree on what the drill recorded. An absent ConfigMap, or one with no `last-success`, is a
drill that has never passed, and every reader reports it as one.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field

CONFIGMAP = "longhorn-restore-drill-stamps"
# `--ignore-not-found` turns an absent ConfigMap into rc 0 with empty stdout, so every non-zero
# exit is a read that failed. Without it, NotFound would share rc 1 with a Forbidden or a timeout,
# and telling them apart would mean matching kubectl's stderr wording.
CONFIGMAP_GET_ARGS = ("get", "configmap", CONFIGMAP, "--ignore-not-found", "-o", "json")
LAST_SUCCESS = "last-success"


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
    `seen_default` is the time a candidate with no entry is judged from.
    """

    where: str
    last_success: str | None
    candidates: list[str] = field(default_factory=list)
    seen: dict[str, float] = field(default_factory=dict)
    seen_default: float | None = None
    success: dict[str, str] = field(default_factory=dict)
    excluded_oversize: list[tuple[str, int]] = field(default_factory=list)
    excluded_nopvc: list[str] = field(default_factory=list)


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


def from_configmap(doc: dict | None, namespace: str) -> DrillStamps:
    """The drill's state from its ConfigMap, `doc`, which is None when the ConfigMap does not exist.

    An absent ConfigMap, or one with no `last-success`, gives `last_success` None, which check 7
    and the upgrade gate report as a drill that has never passed. A candidate with no
    `seen.<pvc>` takes the ConfigMap's creationTimestamp as its seen time: check 8 then flags it
    once the coverage window passes instead of skipping it forever.
    """
    where = "configmap %s/%s key %s" % (namespace, CONFIGMAP, LAST_SUCCESS)
    if doc is None:
        return DrillStamps(where=where, last_success=None)
    data = doc.get("data") or {}
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
    last_success = data.get(LAST_SUCCESS)
    return DrillStamps(
        where=where,
        last_success=None if last_success is None else last_success.rstrip("\n"),
        candidates=candidates,
        seen=seen,
        seen_default=_created_at(doc),
        success=success,
        excluded_oversize=_parse_oversize(_lines(data.get("excluded_oversize"))),
        excluded_nopvc=_lines(data.get("excluded_nopvc")),
    )
