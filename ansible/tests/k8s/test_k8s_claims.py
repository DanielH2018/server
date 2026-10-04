"""`k8s_claims`: the PersistentVolumeClaims `k8s/manifests` renders from a role's defaults.

A role declares `k8s_claims: [{name, size, storage_class}]` and `k8s/manifests` renders each
entry from `ansible/templates/claim-default.yaml.j2` to `claim-<name>.yaml` in the role's own
directory. The offline harnesses (`_k8s_render`, `validate/k8s_manifests.py`) reach the same
claims through `lib.k8s_roles.claim_contexts`, because one template rendered per entry is
invisible to a walk over the templates a role ships. These tests hold the two paths together,
name the claims they must find, and refuse a claim with two creators.

Run: uv run pytest ansible/tests/k8s/test_k8s_claims.py
"""

from collections import Counter
from pathlib import Path

from _helpers import (
    ANSIBLE,
    K8S_ROLES,
    load_defaults,
    load_tasks,
    render_expr,
    task_named,
    walk_tasks,
)
from _k8s_render import rendered_docs
from lib.k8s_roles import CLAIM_TEMPLATE

MANIFESTS = ANSIBLE / "roles/k8s/manifests"

# Named members, with the live spec each must keep: a claim's storage class is immutable and
# its size can only grow, so a converted claim that renders differently fails its next apply.
_KNOWN_CLAIMS = {
    ("homelab", "freshrss-config"): ("longhorn", "2Gi"),
    ("homelab", "zigbee2mqtt-data"): ("longhorn", "2Gi"),
}


def _pvcs(docs):
    return [
        (role, name, doc)
        for role, name, doc in docs
        if doc.get("kind") == "PersistentVolumeClaim"
    ]


def _key(doc) -> tuple[str | None, str | None]:
    meta = doc.get("metadata") or {}
    return meta.get("namespace"), meta.get("name")


def duplicate_claims(pvcs) -> dict[tuple[str | None, str | None], int]:
    """`(namespace, name)` -> count, for every claim rendered more than once."""
    counts = Counter(_key(doc) for _role, _name, doc in pvcs)
    return {key: n for key, n in counts.items() if n > 1}


def test_the_render_task_renders_the_template_the_harnesses_render():
    task = task_named(load_tasks(MANIFESTS / "tasks/main.yml"), "Render volume claims")
    assert task["ansible.builtin.template"]["src"].endswith(
        f"/templates/{CLAIM_TEMPLATE.name}"
    )
    assert task["loop_control"]["loop_var"] == "manifests_claim"
    assert task["ansible.builtin.template"]["dest"].endswith(
        "/claim-{{ manifests_claim.name }}.yaml"
    )


def test_the_claim_file_list_names_what_the_render_task_writes():
    """The prune keep-set and the digest read `manifests_claim_files`, not the render loop."""
    task = task_named(
        load_tasks(MANIFESTS / "tasks/main.yml"), "Name the volume claim files"
    )
    expr = task["ansible.builtin.set_fact"]["manifests_claim_files"]
    claims = [{"name": "a-config"}, {"name": "b-data"}]
    assert render_expr(expr, k8s_claims=claims) == [
        "claim-a-config.yaml",
        "claim-b-data.yaml",
    ]
    assert render_expr(expr) == []


def test_known_claims_render_from_k8s_claims_with_their_live_spec():
    found = {
        _key(doc): (
            doc["spec"]["storageClassName"],
            doc["spec"]["resources"]["requests"]["storage"],
        )
        for _role, name, doc in _pvcs(rendered_docs())
        if name == CLAIM_TEMPLATE.name
    }
    missing = {k: v for k, v in _KNOWN_CLAIMS.items() if found.get(k) != v}
    assert not missing, f"k8s_claims no longer renders {missing}; found {found}"


def test_a_claim_rendered_twice_is_flagged():
    doc = {
        "kind": "PersistentVolumeClaim",
        "metadata": {"namespace": "homelab", "name": "x"},
    }
    assert duplicate_claims(
        [("a", "pvc.yaml.j2", doc), ("a", CLAIM_TEMPLATE.name, doc)]
    ) == {("homelab", "x"): 2}
    assert duplicate_claims([("a", "pvc.yaml.j2", doc)]) == {}


def test_no_claim_has_two_creators():
    """A role's own PVC template and a `k8s_claims` entry naming one claim both apply it.

    `kubectl apply` reports the second over the first as success, so the two templates drift
    apart silently. `k8s/volume-claim` includes are outside the render and are guarded by
    `test_volume_claim_pvc_path_collision.py`.
    """
    found = duplicate_claims(_pvcs(rendered_docs()))
    assert not found, f"claims rendered by more than one template: {found}"


def _sweep_task() -> dict:
    return task_named(
        load_tasks(MANIFESTS / "tasks/main.yml"),
        "Remove the retired volume-claim staging directory",
    )


def _sweeps(**context) -> bool:
    """Whether the sweep's `when:` holds for a real deploy of a role with this context."""
    context.setdefault("k8s_no_mutate", False)
    context.setdefault("manifests_claim_files", [])
    return all(
        render_expr("{{ " + cond + " }}", **context) for cond in _sweep_task()["when"]
    )


def test_the_sweep_removes_the_sibling_volume_claim_staged_into():
    module = _sweep_task()["ansible.builtin.file"]
    assert module["state"] == "absent"
    assert module["path"] == "/etc/rancher/k3s/manifests/{{ manifests_service }}-claims"


def test_the_sweep_runs_for_a_role_with_k8s_claims_or_its_own_pvc():
    assert _sweeps(manifests_claim_files=["claim-a-config.yaml"])
    assert _sweeps(manifests_files=["pvc.yaml", "deployment.yaml"])


def test_the_sweep_skips_a_role_with_neither_and_any_dry_run():
    assert not _sweeps(manifests_files=["server-pvc.yaml", "deployment.yaml"])
    assert not _sweeps()
    assert not _sweeps(
        manifests_claim_files=["claim-a-config.yaml"], k8s_no_mutate=True
    )


def _include(task: dict, role: str) -> dict | None:
    include = task.get("ansible.builtin.include_role") or {}
    return task.get("vars") or {} if include.get("name") == role else None


def sweep_collisions(roles_dir: Path) -> tuple[set[str], dict[str, str]]:
    """The roles the sweep runs for, and those that still stage into the swept directory.

    A role is swept when its defaults declare `k8s_claims` or its manifests include lists
    `pvc.yaml`. One that also includes `k8s/volume-claim` under its own `manifests_service`
    has the claim that include stages deleted on every deploy.
    """
    swept: set[str] = set()
    collisions: dict[str, str] = {}
    for tasks_file in sorted(roles_dir.glob("*/tasks/*.yml")):
        role = tasks_file.parent.parent
        tasks = list(walk_tasks(load_tasks(tasks_file)))
        claimed = {
            str(v.get("volume_claim_service"))
            for t in tasks
            if (v := _include(t, "k8s/volume-claim")) is not None
        }
        for t in tasks:
            v = _include(t, "k8s/manifests")
            if v is None:
                continue
            files = v.get("manifests_files", [])
            if not (load_defaults(role).get("k8s_claims") or "pvc.yaml" in files):
                continue
            service = str(v.get("manifests_service"))
            swept.add(service)
            if service in claimed:
                collisions[service] = str(tasks_file.relative_to(roles_dir))
    return swept, collisions


def test_a_swept_role_still_staging_through_volume_claim_is_flagged(tmp_path):
    (tmp_path / "feed" / "defaults").mkdir(parents=True)
    (tmp_path / "feed" / "defaults" / "main.yml").write_text("---\n")
    tasks = tmp_path / "feed" / "tasks" / "main.yml"
    tasks.parent.mkdir()
    tasks.write_text(
        """
- name: Create the feed claim
  ansible.builtin.include_role:
    name: k8s/volume-claim
  vars:
    volume_claim_service: feed
- name: Deploy feed
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: feed
    manifests_files: [pvc.yaml, deployment.yaml]
"""
    )
    assert sweep_collisions(tmp_path) == ({"feed"}, {"feed": "feed/tasks/main.yml"})


def test_no_swept_role_still_stages_through_volume_claim():
    swept, collisions = sweep_collisions(K8S_ROLES)
    # Named members: a census that finds neither is reading the wrong path or key.
    assert {"freshrss", "wg-easy"} <= swept, swept
    assert not collisions, (
        f"the claims-directory sweep deletes what k8s/volume-claim stages for {collisions}; "
        "convert the role to k8s_claims fully rather than keeping both"
    )
