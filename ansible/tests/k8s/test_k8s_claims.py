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

from _helpers import ANSIBLE, load_tasks, load_yaml, render_expr, task_named
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
    expr = load_yaml(MANIFESTS / "defaults/main.yml")["manifests_claim_files"]
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
