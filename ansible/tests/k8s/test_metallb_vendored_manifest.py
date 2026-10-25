"""The vendored MetalLB manifest declares the pod-template annotations the live pods carry.

Only `kubectl-client-side-apply` owns `prometheus.io/port` and `prometheus.io/scrape` on the
controller and speaker pod templates live. Flux's takeover removes a template field the render
does not declare, which rolls the controller and every speaker pod at the promote (H1.3). A
refresh to a release that drops them would do the same on the next bump, so the check runs on the
file, not only on the census before the flip.

Run: uv run pytest ansible/tests/k8s/test_metallb_vendored_manifest.py
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
    # fact: ansible/roles/k8s/metallb/CLAUDE.md#At a glance
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
