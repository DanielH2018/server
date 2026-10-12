"""One observability config edit must roll one observability workload.

The role sets `manifests_rollout: ''`, so `k8s/manifests` neither waits nor restarts for it,
and the role does both itself. A restart gated on `manifests_render is changed` would be a
per-ROLE signal that fires when ANY of the role's manifests changed, restarting every workload
for an edit only one of them reads.

The role rolls per workload instead, and this module guards the ways that goes quiet:

  * **A ConfigMap edit rolls its own pod, through a `checksum/config` annotation.** Lose the
    annotation and the edit reaches the ConfigMap and never the pod, with the deploy fully
    green — the exact failure the restart task was written for in the first place.
  * **A Secret edit still needs the restart task**, because hashing a Secret's bytes into a
    world-readable annotation would open a read path over it. Each workload that reads one
    names the Secret's rendered file in `restart_secrets`; a workload that gains a Secret input
    and does not name its file is silently never restarted for a rotation.
  * **The annotation hashes less than the ConfigMap.** Each template captures ONE `data:` key's
    body into the variable it hashes, except grafana's, which captures the whole `data:` block.
    A second key added to one of the other four lands outside the capture: the ConfigMap
    changes, the pod template does not, and the annotation is present throughout. That blinds
    the gate, so the key count is pinned below.
  * **The rule files are the one ConfigMap no annotation hashes.** Prometheus reloads them in
    place (`DECIDED: rules reload in place` in prometheus.yaml.j2), so a rule edit must not roll
    the pod. That holds only while Prometheus runs with `--config.auto-reload` and mounts the
    ConfigMap as a whole directory; without either, a rule edit reaches the ConfigMap and never
    the evaluation. `_rules_reload_problems` pins both in place of the key count.

`restart_on` decides what the release record expects (`rollouts[].restart`,
`k8s/manifests/tasks/release_stamp.yml`). Every entry declares `[]`: a `secret` trigger there
would hold a reader to a roll that another Secret file's edit never made, and `probe.py health
observability` would fail it as NOT ROLLED. The role's amend raises the record for the
workloads its restart rolled.
"""

import copy

import pytest

from _helpers import REPO, load_defaults
from _k8s_render import pod_spec, pod_template, rendered_docs


_ROLE = "observability"

# The census must find these. A renamed template or a moved role would otherwise leave every
# loop below iterating nothing, and an `all(...)` over nothing passes.
_EXPECTED_WORKLOADS = frozenset(
    {
        "loki",
        "prometheus",
        "kube-state-metrics",
        "tempo",
        "otel-collector",
        "grafana",
        "alertmanager",
    }
)
_EXPECTED_SECRET_READERS = frozenset({"prometheus", "grafana", "alertmanager"})

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

# The ConfigMap Prometheus reloads in place rather than hashing, exempt from the key count above.
# One key per rule group, so its count moves with every rule PR.
_RULES_CONFIGMAP = "prometheus-rules"
_RULES_RELOAD_FLAG = "--config.auto-reload"

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


def _pod_annotations(workload):
    return (pod_template(workload).get("metadata") or {}).get("annotations") or {}


def _mounted_configmaps(workload):
    """ConfigMap names the workload's pod mounts as a volume."""
    return {
        (volume.get("configMap") or {}).get("name")
        for volume in pod_spec(workload).get("volumes") or []
        if volume.get("configMap")
    }


def _secret_inputs(workload):
    """Secret names the pod reads, as a volume or through `env.valueFrom.secretKeyRef`."""
    spec = pod_spec(workload)
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


def _declared():
    declared = load_defaults(REPO / "ansible/roles/k8s" / _ROLE)
    return {
        entry["name"]: entry for entry in declared["observability_stabilise_workloads"]
    }


def _secret_files():
    """Each role Secret's name, mapped to the rendered file that carries it."""
    return {
        doc["metadata"]["name"]: template.removesuffix(".j2")
        for role, template, doc in rendered_docs()
        if role == _ROLE and doc and doc.get("kind") == "Secret"
    }


def _restart_declaration_problems(workloads, declared, secret_files):
    """Why a workload's restart declaration disagrees with the Secrets its pod reads."""
    problems = []
    for name, workload in sorted(workloads.items()):
        entry = declared.get(name)
        if entry is None:
            problems.append(
                f"{name} is not in observability_stabilise_workloads, so nothing restarts it "
                "for a Secret edit and the play gate never soaks it."
            )
            continue
        if entry.get("restart_on") != []:
            problems.append(
                f"{name} declares restart_on: {entry.get('restart_on')}. Every entry needs []: "
                "the shared role's signals are per ROLE, so any trigger restarts it, or holds "
                "the release record to a restart, for another workload's edit."
            )
        reads = sorted(
            secret_files[secret]
            for secret in _secret_inputs(workload)
            if secret in secret_files
        )
        names = sorted(entry.get("restart_secrets", []))
        if names != reads:
            problems.append(
                f"{name} reads the Secret files {reads} and its restart_secrets names {names}. "
                "A file it reads and does not name never restarts it for a rotation; a file it "
                "names and does not read restarts it for another workload's edit."
            )
    return problems


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
    assert set(checked) >= _EXPECTED_WORKLOADS - {
        "kube-state-metrics",
        "alertmanager",
    }, (
        f"only {sorted(checked)} were checked. kube-state-metrics renders no ConfigMap, and "
        "alertmanager reads its whole config from a Secret, so neither mounts a role "
        "ConfigMap; any other absence means the mount was not recognised."
    )


def test_every_workload_reading_a_role_secret_names_its_file_in_restart_secrets():
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    workloads = _workloads()
    secret_files = _secret_files()
    assert secret_files, "observability renders no Secret — the census is vacuous."

    readers = {
        name
        for name, workload in workloads.items()
        if _secret_inputs(workload) & set(secret_files)
    }
    assert readers == _EXPECTED_SECRET_READERS, (
        f"the workloads reading a observability Secret are {sorted(readers)}, not "
        f"{sorted(_EXPECTED_SECRET_READERS)}. Update restart_secrets and this set together."
    )
    assert _restart_declaration_problems(workloads, _declared(), secret_files) == []


def test_a_reader_whose_restart_secrets_omits_its_file_is_flagged():
    declared = copy.deepcopy(_declared())
    declared["alertmanager"]["restart_secrets"] = []
    problems = _restart_declaration_problems(_workloads(), declared, _secret_files())
    assert len(problems) == 1 and "alertmanager-secret.yaml" in problems[0], problems


def test_a_secret_trigger_in_restart_on_is_flagged():
    declared = copy.deepcopy(_declared())
    declared["grafana"]["restart_on"] = ["secret"]
    problems = _restart_declaration_problems(_workloads(), declared, _secret_files())
    assert len(problems) == 1 and "restart_on" in problems[0], problems


def test_no_role_configmap_grew_a_key_the_annotation_does_not_hash():
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    counts = {
        (doc.get("metadata") or {}).get("name"): len(doc.get("data") or {})
        for doc in _role_docs()
        if doc.get("kind") == "ConfigMap"
        and (doc.get("metadata") or {}).get("name") != _RULES_CONFIGMAP
    }
    assert counts == _CONFIGMAP_DATA_KEYS, (
        f"observability's ConfigMap data keys are {counts}, not {_CONFIGMAP_DATA_KEYS}. Each "
        "template hashes one `{% set %}` capture into its pod template; a key added outside "
        "that capture changes the ConfigMap without moving the pod template, so the edit "
        "reaches the object and never the pod. Widen the capture, then update this map."
    )


def _rules_reload_problems(workload):
    """Why `workload` would not reload a rule edit it mounts, or nothing when it would.

    A workload that mounts no `prometheus-rules` volume has nothing to reload.
    """
    spec = pod_spec(workload)
    volumes = {
        volume.get("name")
        for volume in spec.get("volumes") or []
        if (volume.get("configMap") or {}).get("name") == _RULES_CONFIGMAP
    }
    problems = []
    for container in spec.get("containers") or []:
        mounts = [
            m for m in container.get("volumeMounts") or [] if m.get("name") in volumes
        ]
        if not mounts:
            continue
        if _RULES_RELOAD_FLAG not in (container.get("args") or []):
            problems.append(
                f"{container.get('name')} mounts {_RULES_CONFIGMAP} without "
                f"{_RULES_RELOAD_FLAG}, and no annotation hashes that ConfigMap, so a rule "
                "edit never reaches the evaluation."
            )
        problems += [
            f"{container.get('name')} mounts {_RULES_CONFIGMAP} with subPath "
            f"{m['subPath']!r}, and a subPath mount never sees a ConfigMap update."
            for m in mounts
            if m.get("subPath")
        ]
    return problems


def test_prometheus_reloads_the_rules_it_mounts_is_clean():
    # fact: ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart
    prometheus = _workloads()["prometheus"]
    # Non-vacuity: Prometheus mounts the rules, so the predicate has a subject.
    assert any(
        (volume.get("configMap") or {}).get("name") == _RULES_CONFIGMAP
        for volume in pod_spec(prometheus).get("volumes") or []
    ), f"prometheus no longer mounts {_RULES_CONFIGMAP}; this test has no subject."
    assert _rules_reload_problems(prometheus) == []


def _without(workload, *, flag=False, add_subpath=False):
    """A copy of `workload` with the reload flag dropped or a subPath added to the rules mount."""
    workload = copy.deepcopy(workload)
    for container in pod_spec(workload)["containers"]:
        if flag:
            container["args"] = [
                a for a in container["args"] if a != _RULES_RELOAD_FLAG
            ]
        if add_subpath:
            for mount in container["volumeMounts"]:
                if mount["name"] == _RULES_CONFIGMAP:
                    mount["subPath"] = "alerting-meta.yaml"
    return workload


def test_prometheus_mounting_the_rules_without_the_reload_flag_is_flagged():
    problems = _rules_reload_problems(_without(_workloads()["prometheus"], flag=True))
    assert len(problems) == 1 and _RULES_RELOAD_FLAG in problems[0], problems


def test_prometheus_mounting_the_rules_by_subpath_is_flagged():
    problems = _rules_reload_problems(
        _without(_workloads()["prometheus"], add_subpath=True)
    )
    assert len(problems) == 1 and "subPath" in problems[0], problems


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
