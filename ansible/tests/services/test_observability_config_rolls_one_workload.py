"""One observability config edit must roll one observability workload.

The role sets `manifests_rollout: ''`, so `k8s/manifests` neither waits nor restarts for it,
and the role does both itself. Until 2026-09-28 its restart task looped over all six workloads
gated on `manifests_render is changed` — a per-ROLE signal that fires when ANY of the role's
eight manifests changed. `prometheus.yaml.j2` alone changed in 49 commits in 90 days, and each
of those restarted Loki, Tempo, kube-state-metrics, the collector and Grafana for an edit none
of them reads (issue #2858).

The replacement has two halves, and this module guards both against the ways they go quiet:

  * **A ConfigMap edit rolls its own pod, through a `checksum/config` annotation.** Lose the
    annotation and the edit reaches the ConfigMap and never the pod, with the deploy fully
    green — the exact failure the restart task was written for in the first place.
  * **A Secret edit still needs the restart task**, because hashing a Secret's bytes into a
    world-readable annotation would open a read path over it. The workloads that read one
    declare `restart_on: [secret]`; a workload that gains a Secret input and does not is
    silently never restarted for a rotation.
  * **The annotation hashes less than the ConfigMap.** Each template captures ONE `data:` key's
    body into the variable it hashes, except grafana's, which captures the whole `data:` block.
    A second key added to one of the other four lands outside the capture: the ConfigMap
    changes, the pod template does not, and the annotation is present throughout. That is the
    original bug with the gate blinded, so the key count is pinned below.

`restart_on` also decides what the release record expects (`rollouts[].restart`,
`k8s/manifests/tasks/release_stamp.yml`), so a wrong value fails `probe.py health observability`
with NOT ROLLED rather than going quiet. That half is the loud one; these two are not.
"""

import pytest

from _helpers import REPO, load_defaults
from _k8s_render import rendered_docs


_ROLE = "observability"

# The census must find these. A renamed template or a moved role would otherwise leave every
# loop below iterating nothing, and an `all(...)` over nothing passes.
_EXPECTED_WORKLOADS = frozenset(
    {"loki", "prometheus", "kube-state-metrics", "tempo", "otel-collector", "grafana"}
)
_EXPECTED_SECRET_READERS = frozenset({"prometheus", "grafana"})

# How many `data:` keys each role-rendered ConfigMap has, and so how much of it the pod
# template's hash covers. Adding a key to any but grafana-provisioning needs the template's
# `{% set %}` capture widened in the same edit — see this module's docstring.
_CONFIGMAP_DATA_KEYS = {
    "prometheus-config": 1,
    "loki-config": 1,
    "tempo-config": 1,
    "otel-collector-config": 1,
    "grafana-provisioning": 2,
}

_WORKLOAD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}
_CHECKSUM_ANNOTATION = "checksum/config"


def _role_docs():
    return [doc for role, _tpl, doc in rendered_docs() if role == _ROLE and doc]


def _names_of_kind(docs, kind):
    return {
        (doc.get("metadata") or {}).get("name")
        for doc in docs
        if doc.get("kind") == kind
    }


def _pod_spec(workload):
    return ((workload.get("spec") or {}).get("template") or {}).get("spec") or {}


def _pod_annotations(workload):
    template = (workload.get("spec") or {}).get("template") or {}
    return (template.get("metadata") or {}).get("annotations") or {}


def _mounted_configmaps(workload):
    """ConfigMap names the workload's pod mounts as a volume."""
    return {
        (volume.get("configMap") or {}).get("name")
        for volume in _pod_spec(workload).get("volumes") or []
        if volume.get("configMap")
    }


def _secret_inputs(workload):
    """Secret names the pod reads, as a volume or through `env.valueFrom.secretKeyRef`."""
    spec = _pod_spec(workload)
    names = {
        (volume.get("secret") or {}).get("secretName")
        for volume in spec.get("volumes") or []
        if volume.get("secret")
    }
    for container in spec.get("containers") or []:
        for entry in container.get("env") or []:
            ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref.get("name"):
                names.add(ref["name"])
    return {name for name in names if name}


def _workloads():
    docs = _role_docs()
    return {
        (doc.get("metadata") or {}).get("name"): doc
        for doc in docs
        if doc.get("kind") in _WORKLOAD_KINDS
    }


def _restart_on():
    declared = load_defaults(REPO / "ansible/roles/k8s" / _ROLE)
    return {
        entry["name"]: entry.get("restart_on")
        for entry in declared["observability_stabilise_workloads"]
    }


def test_the_census_finds_every_workload_the_role_renders():
    assert set(_workloads()) == _EXPECTED_WORKLOADS


def test_a_workload_mounting_a_role_configmap_carries_the_checksum_annotation():
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    docs = _role_docs()
    own_configmaps = _names_of_kind(docs, "ConfigMap")
    assert own_configmaps, "observability renders no ConfigMap — the census is vacuous."

    checked = []
    for name, workload in sorted(_workloads().items()):
        mounted = _mounted_configmaps(workload) & own_configmaps
        if not mounted:
            continue
        checked.append(name)
        assert _CHECKSUM_ANNOTATION in _pod_annotations(workload), (
            f"{name} mounts {sorted(mounted)}, which this role renders, and its pod template "
            f"carries no {_CHECKSUM_ANNOTATION} annotation. The role skips the shared "
            "rollout-restart, so a ConfigMap edit would reach the object and never the pod, "
            "with the deploy fully green."
        )
    assert set(checked) >= _EXPECTED_WORKLOADS - {"kube-state-metrics"}, (
        f"only {sorted(checked)} were checked. kube-state-metrics is the one workload here "
        "that renders no ConfigMap; any other absence means the mount was not recognised."
    )


def test_every_workload_reading_a_role_secret_declares_restart_on_secret():
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    docs = _role_docs()
    own_secrets = _names_of_kind(docs, "Secret")
    assert own_secrets, "observability renders no Secret — the census is vacuous."

    readers = {
        name
        for name, workload in _workloads().items()
        if _secret_inputs(workload) & own_secrets
    }
    assert readers == _EXPECTED_SECRET_READERS, (
        f"the workloads reading a observability Secret are {sorted(readers)}, not "
        f"{sorted(_EXPECTED_SECRET_READERS)}. Update restart_on and this set together."
    )

    restart_on = _restart_on()
    for name in sorted(_workloads()):
        triggers = restart_on.get(name)
        assert triggers is not None, (
            f"{name} has no restart_on in observability_stabilise_workloads, so it falls back to "
            "all three signals and the role restarts it for any manifest change again."
        )
        if name in readers:
            assert "secret" in triggers, (
                f"{name} reads {sorted(_secret_inputs(_workloads()[name]) & own_secrets)} and "
                f"declares restart_on: {triggers}. A Secret's bytes are not hashed into the "
                "pod template, so nothing else would restart it for a rotation."
            )
        else:
            assert triggers == [], (
                f"{name} reads no Secret this role renders but declares restart_on: "
                f"{triggers}. Its ConfigMap is hashed into its own pod template, so the apply "
                "rolls it; a restart trigger here restarts it for another workload's edit."
            )


def test_no_role_configmap_grew_a_key_the_annotation_does_not_hash():
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    counts = {
        (doc.get("metadata") or {}).get("name"): len(doc.get("data") or {})
        for doc in _role_docs()
        if doc.get("kind") == "ConfigMap"
    }
    assert counts == _CONFIGMAP_DATA_KEYS, (
        f"observability's ConfigMap data keys are {counts}, not {_CONFIGMAP_DATA_KEYS}. Each "
        "template hashes one `{% set %}` capture into its pod template; a key added outside "
        "that capture changes the ConfigMap without moving the pod template, so the edit "
        "reaches the object and never the pod. Widen the capture, then update this map."
    )


@pytest.mark.parametrize(
    "volumes,expected",
    [
        ([{"name": "c", "configMap": {"name": "loki-config"}}], {"loki-config"}),
        ([{"name": "d", "emptyDir": {}}], set()),
    ],
)
def test_mounted_configmaps_reads_only_configmap_volumes(volumes, expected):
    assert (
        _mounted_configmaps({"spec": {"template": {"spec": {"volumes": volumes}}}})
        == expected
    )


def test_secret_inputs_finds_both_a_volume_and_an_env_reference():
    workload = {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [
                        {"name": "s", "secret": {"secretName": "scrape-creds"}}
                    ],
                    "containers": [
                        {
                            "env": [
                                {
                                    "name": "PW",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "grafana-admin",
                                            "key": "p",
                                        }
                                    },
                                },
                                {"name": "PLAIN", "value": "x"},
                            ]
                        }
                    ],
                }
            }
        }
    }
    assert _secret_inputs(workload) == {"scrape-creds", "grafana-admin"}


def test_secret_inputs_is_empty_for_a_pod_that_reads_none():
    workload = {
        "spec": {
            "template": {
                "spec": {
                    "volumes": [{"name": "c", "configMap": {"name": "loki-config"}}],
                    "containers": [{"env": [{"name": "PLAIN", "value": "x"}]}],
                }
            }
        }
    }
    assert _secret_inputs(workload) == set()
