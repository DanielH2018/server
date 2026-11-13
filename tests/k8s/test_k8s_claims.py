"""`k8s_claims`: the PersistentVolumeClaims the renderer renders from a role's defaults.

A role declares `k8s_claims: [{name, size, storage_class, access_modes?, namespace?}]`, and each
entry renders from `k8s/templates/claim-default.yaml.j2` to `claim-<name>.yaml`. The offline
harnesses (`_k8s_render`, `validate/k8s_manifests.py`) reach the claims through
`lib.k8s_roles.claim_contexts`. These tests name the claims they must find and refuse a claim
with two creators.

Run: uv run pytest tests/k8s/test_k8s_claims.py
"""

from collections import Counter

from _k8s_render import rendered_docs
from lib.k8s_roles import CLAIM_TEMPLATE


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
    # The entries that set `namespace`: a render that dropped it would create four empty
    # claims in homelab beside the live ones.
    ("observability", "loki-data"): ("longhorn-nobackup", "10Gi"),
    ("observability", "prometheus-data"): ("longhorn-nobackup", "12Gi"),
    ("observability", "grafana-data"): ("longhorn-nobackup", "1Gi"),
    ("observability", "tempo-data"): ("longhorn-nobackup", "5Gi"),
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


def claims_from_other_templates(pvcs) -> list[tuple[str, str]]:
    """(role, template) for every claim rendered by a template other than the claim template."""
    return sorted(
        (role, name) for role, name, _doc in pvcs if name != CLAIM_TEMPLATE.name
    )


def test_a_claim_from_a_roles_own_template_is_flagged():
    doc = {"kind": "PersistentVolumeClaim", "metadata": {"name": "x"}}
    assert claims_from_other_templates([("a", "pvc.yaml.j2", doc)]) == [
        ("a", "pvc.yaml.j2")
    ]
    assert claims_from_other_templates([("a", CLAIM_TEMPLATE.name, doc)]) == []


def test_every_claim_renders_from_k8s_claims():
    """`k8s_claims` is the one path that creates a claim (#3387); a role template is not.

    A claim outside it misses the snapshot-ordering apply and the Longhorn tier census
    that read `k8s_claims`.
    """
    pvcs = _pvcs(rendered_docs())
    assert {("observability", "grafana-data"), ("media-volume", "media-data")} <= {
        (role, doc["metadata"]["name"]) for role, _name, doc in pvcs
    }
    assert not claims_from_other_templates(pvcs), (
        "these templates render a PersistentVolumeClaim; declare it in the role's "
        f"k8s_claims instead: {claims_from_other_templates(pvcs)}"
    )
