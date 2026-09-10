#!/usr/bin/env python3
"""A role that calls k8s/volume-claim must not also stage its own `pvc.yaml`.

`k8s/volume-claim` renders the claim it owns to `/etc/rancher/k3s/manifests/<svc>/pvc.yaml`.
`k8s/manifests` renders every entry of `manifests_files` into that same directory, so a role
that lists `pvc.yaml` there writes a DIFFERENT claim over the file volume-claim just staged.
The file flips content on every run, `manifests_render` reports `changed`, and the
rollout-restart gate in `roles/k8s/manifests/tasks/main.yml` restarts the workload on every
deploy with nothing changed. Valheim shipped that way until 2026-09-10 (#1550): two restarts
in one evening with players connected, both whole-plane applies by other sessions, both with a
byte-identical pod template.

The invariant is checked per role, with the input it must accept and the one it must reject,
plus a named census so a rename of `tasks/main.yml` or of the role path cannot empty the check.

Run: uv run pytest ansible/tests/k8s/test_volume_claim_pvc_path_collision.py
"""

from pathlib import Path

import pytest
from _helpers import K8S_ROLES, load_tasks, walk_tasks

VOLUME_CLAIM_ROLE = "k8s/volume-claim"
MANIFESTS_ROLE = "k8s/manifests"
OWNED_FILE = "pvc.yaml"


def _included_role(task: dict) -> str:
    include = task.get("ansible.builtin.include_role") or task.get("include_role") or {}
    return str(include.get("name", "")) if isinstance(include, dict) else ""


def _lists_owned_file(task: dict) -> bool:
    """Whether a manifests include stages `pvc.yaml`, for either shape of `manifests_files`.

    A list is checked by membership. A Jinja string (freshrss builds its list conditionally)
    is checked by substring, which over-reports rather than under-reports: the collision is
    then decided by whether the volume-claim include is conditional.
    """
    files = (task.get("vars") or {}).get("manifests_files", [])
    if isinstance(files, list):
        return OWNED_FILE in files
    return OWNED_FILE in str(files)


def colliding_roles(roles_dir: Path) -> dict[str, str]:
    """Role name -> reason, for every role whose two PVC writers share a staged path.

    A conditional volume-claim include is exempt: freshrss guards its own `pvc.yaml` behind the
    inverse of that same condition, so at most one writer runs per deploy.
    """
    found: dict[str, str] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/main.yml")):
        tasks = list(walk_tasks(load_tasks(tasks_file)))
        claim_includes = [t for t in tasks if _included_role(t) == VOLUME_CLAIM_ROLE]
        if not claim_includes or all("when" in t for t in claim_includes):
            continue
        if any(
            _included_role(t) == MANIFESTS_ROLE and _lists_owned_file(t) for t in tasks
        ):
            found[tasks_file.parent.parent.name] = (
                f"includes {VOLUME_CLAIM_ROLE} unconditionally and lists {OWNED_FILE!r} in "
                "manifests_files; both render to manifests/<svc>/pvc.yaml"
            )
    return found


def volume_claim_callers(roles_dir: Path) -> set[str]:
    return {
        f.parent.parent.name
        for f in roles_dir.glob("*/tasks/main.yml")
        if any(
            _included_role(t) == VOLUME_CLAIM_ROLE for t in walk_tasks(load_tasks(f))
        )
    }


# Verified against the tree on 2026-09-10. A census that finds none of these is reading the
# wrong path, not a tree with no callers.
KNOWN_CALLERS = frozenset({"valheim", "navidrome", "freshrss", "jellyfin", "sonarr"})


def _write_role(root: Path, name: str, tasks: str) -> Path:
    tasks_file = root / name / "tasks" / "main.yml"
    tasks_file.parent.mkdir(parents=True)
    tasks_file.write_text(tasks)
    return root


_CLAIM_INCLUDE = """
- name: Create the {svc} config volume claim
  ansible.builtin.include_role:
    name: k8s/volume-claim
  vars:
    volume_claim_service: {svc}
"""

_MANIFESTS_INCLUDE = """
- name: Deploy {svc} to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: {svc}
    manifests_files:
{files}
"""


def _role_text(svc: str, files: list[str]) -> str:
    listed = "\n".join(f"      - {f}" for f in files)
    return _CLAIM_INCLUDE.format(svc=svc) + _MANIFESTS_INCLUDE.format(
        svc=svc, files=listed
    )


def test_role_with_a_differently_named_claim_file_is_clean(tmp_path):
    root = _write_role(
        tmp_path, "game", _role_text("game", ["server-pvc.yaml", "deployment.yaml"])
    )
    assert colliding_roles(root) == {}


def test_role_staging_pvc_yaml_beside_volume_claim_is_flagged(tmp_path):
    root = _write_role(
        tmp_path, "game", _role_text("game", ["pvc.yaml", "deployment.yaml"])
    )
    assert set(colliding_roles(root)) == {"game"}


def test_conditional_volume_claim_include_is_clean(tmp_path):
    guarded = _CLAIM_INCLUDE.format(svc="feed").replace(
        "  ansible.builtin.include_role:",
        "  when: feed_manage_claim | bool\n  ansible.builtin.include_role:",
    )
    listed = "      - pvc.yaml\n      - deployment.yaml"
    root = _write_role(
        tmp_path, "feed", guarded + _MANIFESTS_INCLUDE.format(svc="feed", files=listed)
    )
    assert colliding_roles(root) == {}


def test_census_finds_the_known_callers():
    missing = KNOWN_CALLERS - volume_claim_callers(K8S_ROLES)
    assert not missing, f"volume-claim callers not found in the tree: {sorted(missing)}"


def test_no_role_stages_pvc_yaml_beside_volume_claim():
    found = colliding_roles(K8S_ROLES)
    assert not found, "\n".join(f"{role}: {why}" for role, why in sorted(found.items()))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
