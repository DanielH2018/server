"""`k8s_claims`: the PersistentVolumeClaims `k8s/manifests` renders from a role's defaults.

A role declares `k8s_claims: [{name, size, storage_class, access_modes?}]` and `k8s/manifests` renders each
entry from `ansible/templates/claim-default.yaml.j2` to `claim-<name>.yaml` in the role's own
directory. The offline harnesses (`_k8s_render`, `validate/k8s_manifests.py`) reach the same
claims through `lib.k8s_roles.claim_contexts`, because one template rendered per entry is
invisible to a walk over the templates a role ships. These tests hold the two paths together,
name the claims they must find, and refuse a claim with two creators.

Run: uv run pytest ansible/tests/k8s/test_k8s_claims.py
"""

from collections import Counter

from _helpers import (
    ANSIBLE,
    load_tasks,
    render_expr,
    task_named,
)
from _k8s_render import rendered_docs
from lib.k8s_roles import CLAIM_TEMPLATE

MANIFESTS = ANSIBLE / "roles/k8s/manifests"

# Named members, with the live spec each must keep: a claim's storage class is immutable and
# its size can only grow, so a converted claim that renders differently fails its next apply.
_KNOWN_CLAIMS = {
    ("homelab", "freshrss-config"): ("longhorn", "2Gi"),
    ("homelab", "zigbee2mqtt-data"): ("longhorn", "2Gi"),
    ("homelab", "navidrome-data"): ("longhorn", "2Gi"),
    ("homelab", "prowlarr-config"): ("longhorn", "2Gi"),
    ("homelab", "karakeep-data"): ("longhorn", "4Gi"),
    ("homelab", "karakeep-meili"): ("longhorn-nobackup", "2Gi"),
    ("homelab", "qbittorrent-config"): ("longhorn", "1Gi"),
    ("homelab", "terraria-config"): ("longhorn", "1Gi"),
    ("homelab", "code-server-config"): ("longhorn", "10Gi"),
    ("homelab", "code-server-workspace"): ("longhorn", "1Gi"),
    ("homelab", "n8n-data"): ("longhorn", "4Gi"),
    ("homelab", "n8n-files"): ("longhorn", "1Gi"),
    ("homelab", "valheim-config"): ("longhorn", "5Gi"),
    ("homelab", "valheim-server"): ("longhorn-nobackup", "20Gi"),
    ("homelab", "scrutiny-influxdb-data"): ("longhorn", "2Gi"),
    ("homelab", "scrutiny-web-config"): ("longhorn", "1Gi"),
    ("homelab", "tdarr-server"): ("longhorn", "3Gi"),
    ("homelab", "tdarr-configs"): ("longhorn", "1Gi"),
    ("homelab", "home-assistant-config"): ("longhorn", "4Gi"),
    ("homelab", "terraria-stats-data"): ("longhorn", "1Gi"),
    ("homelab", "valheim-stats-data"): ("longhorn", "1Gi"),
    ("homelab", "uptime-kuma-data"): ("longhorn", "4Gi"),
    ("homelab", "autokuma-data"): ("longhorn", "1Gi"),
    ("homelab", "pi-peer-backup-data"): ("longhorn", "128Mi"),
    ("homelab", "mosquitto-data"): ("longhorn-nobackup", "1Gi"),
    ("homelab", "registry-data"): ("longhorn-nobackup", "10Gi"),
    ("homelab", "loki-homelab-data"): ("longhorn-nobackup", "5Gi"),
    ("homelab", "jellyfin-config"): ("longhorn", "8Gi"),
    ("homelab", "authelia-config"): ("longhorn", "1Gi"),
    ("homelab", "crowdsec-db"): ("longhorn", "1Gi"),
    ("homelab", "pihole-etc"): ("longhorn-nobackup", "2Gi"),
    ("homelab", "pihole-etc-2"): ("longhorn-nobackup", "2Gi"),
    ("homelab", "traefik-acme"): ("longhorn", "128Mi"),
    ("homelab", "wg-easy-config"): ("longhorn", "1Gi"),
    ("homelab", "media-data"): ("media-local", "400Gi"),
}
# The one claim whose `k8s_claims` entry sets `access_modes`. Access modes are immutable, and
# the size/class pair above would pass a render that dropped the entry's modes for the default.
_RWX_CLAIMS = {("homelab", "media-data")}


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
    # The src is the labelling wrapper for an armed role, which renders `manifests_label_src`.
    assert "manifests_label_src" in task["ansible.builtin.template"]["src"]
    assert task["vars"]["manifests_label_src"].endswith(
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


def test_known_claims_render_their_live_access_modes():
    found = {
        _key(doc): doc["spec"]["accessModes"]
        for _role, name, doc in _pvcs(rendered_docs())
        if name == CLAIM_TEMPLATE.name and _key(doc) in _KNOWN_CLAIMS
    }
    wrong = {
        key: modes
        for key, modes in found.items()
        if modes != (["ReadWriteMany"] if key in _RWX_CLAIMS else ["ReadWriteOnce"])
    }
    assert set(found) == set(_KNOWN_CLAIMS), set(_KNOWN_CLAIMS) - set(found)
    assert not wrong, f"claims render the wrong accessModes: {wrong}"


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
    apart silently.
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
    # Under a real deploy manifests_dest_dir is /etc/rancher/k3s/manifests/<service>.
    assert module["path"] == "{{ manifests_dest_dir }}-claims"


def test_the_sweep_runs_for_a_role_with_k8s_claims_or_its_own_pvc():
    assert _sweeps(manifests_claim_files=["claim-a-config.yaml"])
    assert _sweeps(manifests_files=["pvc.yaml", "deployment.yaml"])


def test_the_sweep_skips_a_role_with_neither_and_any_dry_run():
    assert not _sweeps(manifests_files=["server-pvc.yaml", "deployment.yaml"])
    assert not _sweeps()
    assert not _sweeps(
        manifests_claim_files=["claim-a-config.yaml"], k8s_no_mutate=True
    )
