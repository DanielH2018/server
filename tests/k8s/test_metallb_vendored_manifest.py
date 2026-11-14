"""The vendored MetalLB manifest declares the pod-template annotations the live pods carry.

Only `kubectl-client-side-apply` owns `prometheus.io/port` and `prometheus.io/scrape` on the
controller and speaker pod templates live. Flux's takeover removes a template field the render
does not declare, which rolls the controller and every speaker pod at the promote (H1.3). A
refresh to a release that drops them would do the same on the next bump, so the check runs on the
file, not only on the census before the flip.

Run: uv run pytest tests/k8s/test_metallb_vendored_manifest.py
"""

import copy

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

UPSTREAM = K8S_ROLES / "metallb" / "files" / "upstream" / "metallb-native.yaml"
WORKLOADS = (("Deployment", "controller"), ("DaemonSet", "speaker"))
ANNOTATIONS = ("prometheus.io/port", "prometheus.io/scrape")


def _workloads(text: str) -> dict[tuple[str, str], dict]:
    docs = [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def _missing(workload: dict) -> list[str]:
    template = workload["spec"]["template"]["metadata"].get("annotations") or {}
    return [a for a in ANNOTATIONS if a not in template]


def test_the_vendored_file_declares_the_prometheus_template_annotations():
    # fact: k8s/roles/metallb/CLAUDE.md#At a glance
    found = _workloads(UPSTREAM.read_text())
    for member in WORKLOADS:
        assert member in found, f"metallb-native.yaml declares no {member}"
        assert _missing(found[member]) == [], (
            f"{member} drops {_missing(found[member])} from its pod template: the takeover "
            "would remove them live and roll every MetalLB pod"
        )


def test_a_template_without_the_annotations_is_flagged():
    controller = _workloads(UPSTREAM.read_text())[WORKLOADS[0]]
    stripped = copy.deepcopy(controller)
    del stripped["spec"]["template"]["metadata"]["annotations"]["prometheus.io/scrape"]
    assert _missing(controller) == []
    assert _missing(stripped) == ["prometheus.io/scrape"]


PATCHES = K8S_ROLES / "metallb" / "files" / "patches"
RESOURCE_KEYS = (
    ("requests", "cpu"),
    ("requests", "memory"),
    ("limits", "cpu"),
    ("limits", "memory"),
)


def _patched_containers() -> dict[tuple[str, str], dict]:
    """Each upstream workload's containers by (workload, container), patch `resources` merged."""
    workloads = _workloads(UPSTREAM.read_text())
    for path in sorted(PATCHES.glob("*.yaml")) if PATCHES.is_dir() else ():
        patch = yaml_fast.safe_load(path.read_text())
        target = workloads[(patch["kind"], patch["metadata"]["name"])]
        assert patch["metadata"]["namespace"] == target["metadata"]["namespace"], path
        upstream = {
            c["name"]: c for c in target["spec"]["template"]["spec"]["containers"]
        }
        for change in patch["spec"]["template"]["spec"]["containers"]:
            resources = upstream[change["name"]].setdefault("resources", {})
            for section, values in change["resources"].items():
                resources[section] = {**(resources.get(section) or {}), **values}
    return {
        (name, c["name"]): c
        for (kind, name) in WORKLOADS
        for c in workloads[(kind, name)]["spec"]["template"]["spec"]["containers"]
    }


def _missing_keys(container: dict) -> list[str]:
    resources = container.get("resources") or {}
    return [f"{s}.{k}" for s, k in RESOURCE_KEYS if not (resources.get(s) or {}).get(k)]


def test_the_patches_give_every_metallb_container_requests_and_limits():
    # fact: k8s/roles/metallb/CLAUDE.md#At a glance
    # Without all four keys the pod is BestEffort or inherits no default: metallb-system has no
    # LimitRange, and the kubelet evicts a BestEffort speaker first, dropping every VIP.
    containers = _patched_containers()
    assert set(containers) == {("controller", "controller"), ("speaker", "speaker")}
    missing = {member: _missing_keys(c) for member, c in containers.items()}
    assert missing == {member: [] for member in containers}, (
        f"these MetalLB containers lack resource keys: {missing}. Set them in "
        "k8s/roles/metallb/files/patches/."
    )


def test_a_container_missing_a_resource_key_is_flagged():
    sized = {
        "resources": {
            "requests": {"cpu": "10m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "256Mi"},
        }
    }
    partial = copy.deepcopy(sized)
    del partial["resources"]["limits"]["memory"]
    assert _missing_keys(sized) == []
    assert _missing_keys(partial) == ["limits.memory"]
    assert _missing_keys({"name": "speaker"}) == [f"{s}.{k}" for s, k in RESOURCE_KEYS]
