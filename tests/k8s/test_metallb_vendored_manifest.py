"""The vendored MetalLB manifest declares the pod-template annotations the live pods carry.

Only `kubectl-client-side-apply` owns `prometheus.io/port` and `prometheus.io/scrape` on the
controller and speaker pod templates live. Flux's takeover removes a template field the render
does not declare, which rolls the controller and every speaker pod at the promote (H1.3). A
refresh to a release that drops them would do the same on the next bump, so the check runs on the
file, not only on the census before the flip.

The role's two patches give the controller and the speaker a memory request, so neither pod
runs BestEffort (#8326); the tests at the bottom hold each patch equal to its defaults key.

Run: uv run pytest tests/k8s/test_metallb_vendored_manifest.py
"""

import copy

from _helpers import besteffort_workloads, load_defaults
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


ROLE = K8S_ROLES / "metallb"
DEFAULTS = load_defaults(ROLE)
PATCH_KEYS = {
    ("Deployment", "controller"): "k3s_metallb_controller_memory_request",
    ("DaemonSet", "speaker"): "k3s_metallb_speaker_memory_request",
}


def _patches() -> dict[tuple[str, str], dict]:
    return _workloads(
        "\n---\n".join(
            p.read_text() for p in sorted((ROLE / "files" / "patches").glob("*.yaml"))
        )
    )


def test_no_metallb_workload_runs_besteffort():
    # fact: k8s/roles/metallb/CLAUDE.md#At a glance
    # An evicted speaker drops every VIP its node announces (#8326).
    assert besteffort_workloads(ROLE) == []


def test_each_patch_requests_its_defaults_value_and_no_limit():
    # fact: k8s/roles/metallb/CLAUDE.md#At a glance
    # A kustomize patch is copied verbatim and cannot read a variable.
    patches = _patches()
    assert set(patches) == set(PATCH_KEYS)
    upstream = _workloads(UPSTREAM.read_text())
    for member, key in PATCH_KEYS.items():
        (container,) = patches[member]["spec"]["template"]["spec"]["containers"]
        assert container["name"] == member[1]
        assert container["name"] in {
            c["name"]
            for c in upstream[member]["spec"]["template"]["spec"]["containers"]
        }
        assert container["resources"] == {"requests": {"memory": str(DEFAULTS[key])}}


def test_the_unpatched_upstream_file_is_flagged(tmp_path):
    (tmp_path / "files" / "upstream").mkdir(parents=True)
    (tmp_path / "files" / "upstream" / UPSTREAM.name).write_text(UPSTREAM.read_text())
    assert besteffort_workloads(tmp_path) == sorted(PATCH_KEYS)
