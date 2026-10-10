#!/usr/bin/env python3
"""The snapshot reaper's opt-out group list against the StorageClasses that assign them.

`longhorn_reap_logic.OPT_OUT_GROUPS` hardcodes the recurring-job groups that name no
RecurringJob CR by design, because the module is pure stdlib logic with no file or cluster
reads. That makes it a copy of a fact the k3s role declares in a StorageClass's
`recurringJobSelector`, and a copy drifts: a second opt-out class added later would put every
volume it provisions back into the unresolved-owner abort that #3236 was filed for.

The pair here reads the role's own StorageClasses and asserts the two lists agree, so adding an
opt-out class without extending `OPT_OUT_GROUPS` fails here rather than on the cluster.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_reap_opt_out_groups.py
"""

import json

from lib import yaml_fast
from _setup_render import rendered_setup_text

import longhorn_reap_logic as logic
from lib.repo_paths import K3S_FILES


# The classes this sweep must find. Named rather than counted: the glob reads empty the day the
# StorageClasses move directory, and an `all(...)` over nothing passes.
EXPECTED_STORAGECLASS_FILES = frozenset({"longhorn-storageclass-nobackup.yaml"})

RECURRING_JOB_TEMPLATE = "longhorn-recurringjob.yaml.j2"


def _selected_groups() -> dict[str, set[str]]:
    """Every group a `setup/k3s` StorageClass selects, by StorageClass file name.

    `recurringJobSelector` is a JSON string inside the YAML — Longhorn's own parameter format —
    so it is parsed twice. Only `isGroup: true` entries name a group; a `false` one names a job
    directly and cannot be an opt-out.
    """
    selected: dict[str, set[str]] = {}
    for path in sorted(K3S_FILES.glob("*storageclass*.yaml")):
        doc = yaml_fast.safe_load(path.read_text()) or {}
        raw = (doc.get("parameters") or {}).get("recurringJobSelector")
        if not raw:
            continue
        groups = {
            entry["name"] for entry in json.loads(raw) if entry.get("isGroup") is True
        }
        if groups:
            selected[path.name] = groups
    return selected


def _groups_with_a_recurringjob() -> set[str]:
    """Every group a RecurringJob the k3s role applies claims.

    Read from the RENDER, not the template source: the weekly tier's seven shards and the
    groups they claim only exist after the `{% for %}` runs.
    """
    docs = yaml_fast.safe_load_all(rendered_setup_text("k3s", RECURRING_JOB_TEMPLATE))
    claimed: set[str] = set()
    for doc in docs:
        if doc:
            claimed.update((doc.get("spec") or {}).get("groups") or [])
    return claimed


def test_every_jobless_storageclass_group_is_a_known_opt_out() -> None:
    """A StorageClass group with no RecurringJob must be in `OPT_OUT_GROUPS`.

    The accepting half, against the live declarations: `no-backup` is selected by
    `files/longhorn-storageclass-nobackup.yaml`, no RecurringJob claims it, and the reaper
    knows it as an opt-out. A new opt-out class fails here until the list names its group.
    """
    selected = _selected_groups()
    assert set(selected) >= EXPECTED_STORAGECLASS_FILES, (
        "the StorageClass sweep found %s, expected at least %s"
        % (
            sorted(selected),
            sorted(EXPECTED_STORAGECLASS_FILES),
        )
    )
    jobless = {
        group
        for groups in selected.values()
        for group in groups
        if group not in _groups_with_a_recurringjob()
    }
    assert "no-backup" in jobless, (
        "no-backup is expected to be selected by a StorageClass and claimed by no "
        "RecurringJob; found jobless groups %s" % sorted(jobless)
    )
    assert jobless <= logic.OPT_OUT_GROUPS, (
        "these StorageClass groups name no RecurringJob and are not in OPT_OUT_GROUPS, so "
        "every volume they provision trips the unresolved-owner ABORT in the snapshot "
        "reaper: %s" % sorted(jobless - logic.OPT_OUT_GROUPS)
    )


def test_a_group_a_recurringjob_claims_is_not_treated_as_an_opt_out() -> None:
    """The rejecting half: `OPT_OUT_GROUPS` must not swallow a group that has a job.

    An opt-out entry for a group a RecurringJob claims would hide the renamed-job abort for
    every volume in it — the lookup would resolve to the sentinel instead of `""`. The real
    RecurringJob still wins inside `snapshot_owner_map`, and this keeps the list honest too.
    """
    claimed = _groups_with_a_recurringjob()
    assert {"default", "weekly-backup-d3"} <= claimed, (
        "the RecurringJob render claims the groups %s, which does not include `default` and "
        "`weekly-backup-d3` — the parse found nothing to read" % sorted(claimed)
    )
    overlap = claimed & logic.OPT_OUT_GROUPS
    assert not overlap, (
        "these groups have a RecurringJob and so resolve at runtime; listing them as opt-outs "
        "disarms the renamed-job ABORT for their volumes: %s" % sorted(overlap)
    )
