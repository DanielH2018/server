#!/usr/bin/env python3
"""No `manifests_service` may claim a directory some role defers its own manifests into.

`k8s/manifests` renders `manifests_deferred_files` into
`/etc/rancher/k3s/manifests/<manifests_deferred_dir_name>/`, prunes that directory from the same
list, and leaves the apply to the owning role — pihole's instance 2 is the one caller. A role
that later took that directory
name as its own `manifests_service` would prune the manifest pihole applies and sweep it into its
own `kubectl apply -f <dir>/`, which is the single request the deferral exists to escape.

That is the same reservation `<service>-netpol` and `<service>-claims` carry, and
test_no_role_stages_files_in_a_pruned_manifest_dir.py holds those. It cannot hold this one: the
path is built inside `k8s/manifests` from a variable, so its raw-text path reader finds
nothing, and `pihole-instance-2` would be reserved by nothing at all.

Run: uv run pytest ansible/tests/k8s/test_deferred_manifest_dir_is_reserved.py
"""

from pathlib import Path

import pytest
from _helpers import K8S_ROLES, load_tasks, walk_tasks
from test_no_role_stages_files_in_a_pruned_manifest_dir import (
    MANIFESTS_ROLE,
    _included_role,
    owning_roles,
)

# Named rather than counted, so a rename cannot empty the census and leave the invariant below
# passing over nothing (.claude/rules/python-layout.md).
KNOWN_DEFERRED_DIRS = {"pihole-instance-2": "pihole"}


def deferred_dirs(roles_dir: Path) -> dict[str, str]:
    """Deferred directory name -> the role that hands it to `k8s/manifests`."""
    found: dict[str, str] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/*.yml")):
        for task in walk_tasks(load_tasks(tasks_file)):
            if _included_role(task) != MANIFESTS_ROLE:
                continue
            name = str(
                (task.get("vars") or {}).get("manifests_deferred_dir_name", "")
            ).strip()
            if name and "{{" not in name:
                found[name] = tasks_file.parent.parent.name
    return found


def deferred_name_claims(roles_dir: Path) -> dict[str, str]:
    """`manifests_service` -> reason, for every service claiming a deferred directory name."""
    deferred = deferred_dirs(roles_dir)
    return {
        service: (
            f"names the directory {deferred[service]!r} defers its own manifests into, so this "
            "role's prune would delete them and its apply would carry them in one request"
        )
        for service, role in owning_roles(roles_dir).items()
        if service in deferred and deferred[service] != role
    }


_DEFERRER = """---
- name: Deploy gadget to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: gadget
    manifests_files:
      - deployment.yaml
    manifests_deferred_files:
      - deployment-2.yaml
    manifests_deferred_dir_name: gadget-instance-2
"""

_CLAIMER = """---
- name: Deploy {service} to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: {service}
    manifests_files:
      - deployment.yaml
"""


def _write_role(root: Path, name: str, text: str) -> Path:
    tasks_file = root / name / "tasks" / "main.yml"
    tasks_file.parent.mkdir(parents=True)
    tasks_file.write_text(text)
    return root


def test_an_unrelated_service_beside_a_deferrer_is_clean(tmp_path):
    root = _write_role(tmp_path, "gadget", _DEFERRER)
    _write_role(root, "widget", _CLAIMER.format(service="widget"))
    assert deferred_name_claims(root) == {}
    assert deferred_dirs(root) == {"gadget-instance-2": "gadget"}


def test_a_service_claiming_a_deferred_directory_is_flagged(tmp_path):
    root = _write_role(tmp_path, "gadget", _DEFERRER)
    _write_role(root, "second", _CLAIMER.format(service="gadget-instance-2"))
    assert set(deferred_name_claims(root)) == {"gadget-instance-2"}


def test_the_census_finds_the_known_deferred_directories():
    """Non-vacuity: an empty reader makes the invariant below pass over nothing."""
    assert deferred_dirs(K8S_ROLES) == KNOWN_DEFERRED_DIRS


def test_no_manifests_service_claims_a_deferred_manifest_directory():
    found = deferred_name_claims(K8S_ROLES)
    assert not found, "\n".join(
        f"manifests_service {service!r} {why}" for service, why in sorted(found.items())
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
