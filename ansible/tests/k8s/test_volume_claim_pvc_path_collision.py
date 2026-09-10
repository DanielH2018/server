#!/usr/bin/env python3
"""Two invariants on where `k8s/volume-claim`'s claim is staged, and who else creates it.

`k8s/volume-claim` renders the claim it owns under `/etc/rancher/k3s/manifests/`. Two things
about that path have each cost a deploy:

1. **The claim must not be staged in the consuming role's own manifest directory.** That
   directory belongs to `k8s/manifests`, whose prune task deletes every file in it that the
   caller does not name in `manifests_files`/`manifests_secret_files`. No caller names this
   claim, so the prune deleted it on every deploy — a permanently `changed` task on an
   otherwise idempotent run, and a staged claim never re-applied from the role's own
   directory (#1654). The claim now lands in a sibling `<service>-claims/` directory that no
   role's `manifests_service` claims, so no `kubectl apply -f <dir>/` sweeps it.

2. **Exactly one path may create a given claim.** A role that includes `k8s/volume-claim`
   unconditionally AND lists `pvc.yaml` in `manifests_files` has two independently-edited
   manifests declaring one claim name. Valheim shipped that way until 2026-09-10 (#1550):
   both writers targeted the same staged file, its content flipped every run, and the
   rollout-restart gate restarted the server with a byte-identical pod template — two
   restarts in one evening with players connected. The staged-path half of that mechanism is
   gone with invariant 1; the two-creators half is not, so it stays guarded here.

Each invariant is checked with the input it must accept and the one it must reject, plus a
named census so a rename of `tasks/main.yml` or of the role path cannot empty the check.

Run: uv run pytest ansible/tests/k8s/test_volume_claim_pvc_path_collision.py
"""

import re
from pathlib import Path

import pytest
from _helpers import K8S_ROLES, load_tasks, walk_tasks

VOLUME_CLAIM_ROLE = "k8s/volume-claim"
MANIFESTS_ROLE = "k8s/manifests"
OWNED_FILE = "pvc.yaml"

MANIFEST_ROOT = "/etc/rancher/k3s/manifests"
# The directory `k8s/manifests` renders into and prunes, as volume-claim's tasks would spell it.
CONSUMED_DIR = f"{MANIFEST_ROOT}/{{{{ volume_claim_service }}}}"


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


# A path segment is either a Jinja expression — which contains spaces, so `\S+` cannot be used
# here — or ordinary path characters. Prose in the comments (`manifests/<service>/`) matches
# neither and is skipped rather than read as a path.
_PATH_RE = re.compile(
    rf"{re.escape(MANIFEST_ROOT)}(?:/(?:\{{\{{[^}}]*\}}\}}|[A-Za-z0-9._*-])+)+"
)


def staged_paths(claim_tasks: Path) -> set[str]:
    """Every path under the manifest root that volume-claim's tasks name.

    Read from the raw text rather than the parsed tasks, so a `file: path:`, a
    `template: dest:` and the path inside the `kubectl apply` command are all covered by one
    pattern — the render and the apply have to move together, and a check that saw only the
    render would pass while the apply still pointed at the pruned directory.
    """
    return set(_PATH_RE.findall(claim_tasks.read_text()))


def paths_inside_consumed_dir(claim_tasks: Path) -> set[str]:
    """The staged paths that land in the directory `k8s/manifests` prunes."""
    return {p for p in staged_paths(claim_tasks) if p.startswith(f"{CONSUMED_DIR}/")}


def colliding_roles(roles_dir: Path) -> dict[str, str]:
    """Role name -> reason, for every role with two creators for one claim.

    A conditional volume-claim include is exempt: freshrss guards its own `pvc.yaml` behind the
    inverse of that same condition, so at most one creator runs per deploy.
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
                "manifests_files; both declare the same claim name"
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

REAL_CLAIM_TASKS = K8S_ROLES / "volume-claim" / "tasks" / "claim.yml"


# ── invariant 1: the claim is staged outside the pruned directory ────────────────────────────

_OLD_CLAIM_TASKS = f"""---
- name: Render the volume claim
  ansible.builtin.template:
    src: pvc.yaml.j2
    dest: "{CONSUMED_DIR}/pvc.yaml"

- name: Create the PVC
  ansible.builtin.command:
    cmd: "k3s kubectl apply -f {CONSUMED_DIR}/pvc.yaml"
"""

_MOVED_CLAIM_TASKS = _OLD_CLAIM_TASKS.replace(
    CONSUMED_DIR, f"{CONSUMED_DIR}-claims"
).replace('pvc.yaml"', '{{ volume_claim_name }}.yaml"')

# The render moved and the apply did not — the shape a check reading only `dest:` would miss.
_HALF_MOVED_CLAIM_TASKS = _OLD_CLAIM_TASKS.replace(
    f'dest: "{CONSUMED_DIR}/pvc.yaml"', f'dest: "{CONSUMED_DIR}-claims/data.yaml"'
)


def _write_claim_tasks(root: Path, text: str) -> Path:
    path = root / "claim.yml"
    path.write_text(text)
    return path


def test_claim_staged_outside_the_pruned_directory_is_clean(tmp_path):
    assert (
        paths_inside_consumed_dir(_write_claim_tasks(tmp_path, _MOVED_CLAIM_TASKS))
        == set()
    )


def test_claim_staged_in_the_pruned_directory_is_flagged(tmp_path):
    assert paths_inside_consumed_dir(
        _write_claim_tasks(tmp_path, _OLD_CLAIM_TASKS)
    ) == {f"{CONSUMED_DIR}/pvc.yaml"}


def test_apply_left_behind_in_the_pruned_directory_is_flagged(tmp_path):
    assert paths_inside_consumed_dir(
        _write_claim_tasks(tmp_path, _HALF_MOVED_CLAIM_TASKS)
    ) == {f"{CONSUMED_DIR}/pvc.yaml"}


# The staging directory and the claim file, as claim.yml spells them. Named rather than
# counted: an empty or shrunken path set would make the invariant below pass on nothing, and a
# count moving says less than which member went missing.
EXPECTED_STAGED_PATHS = frozenset(
    {
        f"{CONSUMED_DIR}-claims",
        f"{CONSUMED_DIR}-claims/{{{{ volume_claim_name }}}}.yaml",
    }
)


def test_the_real_claim_tasks_name_the_expected_staged_paths():
    """Non-vacuity: the path reader must find claim.yml's real directory and file."""
    missing = EXPECTED_STAGED_PATHS - staged_paths(REAL_CLAIM_TASKS)
    assert not missing, (
        f"{REAL_CLAIM_TASKS} no longer names {sorted(missing)}; the path reader is looking at "
        "the wrong thing, so the invariant below would pass on an empty set"
    )


def test_volume_claim_stages_outside_every_consuming_roles_directory():
    found = paths_inside_consumed_dir(REAL_CLAIM_TASKS)
    assert not found, (
        f"{REAL_CLAIM_TASKS} stages {sorted(found)} inside the directory k8s/manifests "
        "prunes, so the file is deleted on every deploy of the consuming role (#1654)"
    )


# ── invariant 2: exactly one creator per claim ───────────────────────────────────────────────

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


def _write_role(root: Path, name: str, tasks: str) -> Path:
    tasks_file = root / name / "tasks" / "main.yml"
    tasks_file.parent.mkdir(parents=True)
    tasks_file.write_text(tasks)
    return root


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
