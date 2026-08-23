#!/usr/bin/env python3
"""Guards that no k8s role hardcodes `seed_volume_force: true` in its seed-volume call.

The failure this encodes was found armed, not fired (daniel-box, 2026-08-07).

`k8s/seed-volume` is one-shot and idempotent: it writes a `.seeded` marker into the volume
after the copy verifies, and a rerun reads that marker and copies nothing. The whole safety
property lives in one expression (roles/k8s/seed-volume/tasks/main.yml):

    seed_volume_copying: "{{ seed_volume_marker.rc != 0 or seed_volume_force | ... }}"

`seed_volume_force` is the deliberate-re-seed escape hatch, documented in that role's header
as something you pass per run (`-e seed_volume_force=true`). n8n's role instead hardcoded it
in the `include_role` vars, because n8n was the one service in slice 2 with no coexistence
window — its first run was also the cutover, and forcing the copy made a source that moved a
hard failure rather than a warning. That was correct for exactly that one run.

It did not stay correct. Once the cutover completed, the Docker source froze (2026-08-06
17:53) while the cluster copy became the only writer (17:56), so a routine
`ansible-playbook deploy.yml --tags n8n` would quiesce n8n and `tar -x` the stale snapshot
over the live PVC — overwriting database.sqlite and its 218 MB WAL. copy.yml's assert does
catch it, but only after the overwrite: the destination keeps the files the source lacks, so
the counts diverge and it fails loudly having already destroyed the data.

Hardcoding the flag is what turns a one-shot seed into an every-deploy restore, so that is
what this guards. A per-run `-e seed_volume_force=true` is unaffected and still works.

Run: uv run pytest ansible/tests/test_seed_volume_force.py
"""

from pathlib import Path

import yaml
from _helpers import ANSIBLE


K8S_ROLES = ANSIBLE / "roles" / "k8s"


def _seed_volume_callers() -> list[tuple[Path, dict]]:
    """(path, task) for every task that includes the k8s/seed-volume role."""
    callers = []
    for main in sorted(K8S_ROLES.glob("*/tasks/main.yml")):
        tasks = yaml.safe_load(main.read_text()) or []
        for task in tasks:
            include = task.get("ansible.builtin.include_role") or {}
            if include.get("name") == "k8s/seed-volume":
                callers.append((main, task))
    return callers


def test_seed_volume_is_actually_used():
    """A guard over an empty set passes while guarding nothing — so pin the set is non-empty."""
    assert _seed_volume_callers(), (
        f"No role under {K8S_ROLES} includes k8s/seed-volume. Either the role was renamed or "
        "this test's discovery broke — in both cases the tests below silently stopped guarding."
    )


def test_no_role_hardcodes_seed_volume_force():
    """The marker must govern re-seeding; force is a per-run decision, not a role property."""
    forced = [
        str(path.relative_to(ANSIBLE)) if path.is_relative_to(ANSIBLE) else str(path)
        for path, task in _seed_volume_callers()
        if (task.get("vars") or {}).get("seed_volume_force")
    ]

    assert not forced, (
        f"{forced} hardcode seed_volume_force in their k8s/seed-volume call. That bypasses the "
        "`.seeded` marker on EVERY deploy, so each run re-copies the (now stale) Docker source "
        "over the live volume — see this file's docstring for the n8n case. Drop the var and "
        "pass `-e seed_volume_force=true` for a deliberate re-seed instead."
    )
