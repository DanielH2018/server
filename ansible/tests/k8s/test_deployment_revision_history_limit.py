#!/usr/bin/env python3
"""Every Deployment template must pin `revisionHistoryLimit`.

WHY THIS EXISTS. Kubernetes defaults `revisionHistoryLimit` to 10, so an unpinned Deployment
keeps ten scaled-to-zero ReplicaSets as rollback history. Across this cluster that read as 605
ReplicaSets against 68 Deployments and 119 pods on 2026-08-28 — 537 of them empty. Nothing
breaks, but `kubectl get rs -A` stops being usable for reading cluster state, and the count
grows with every image-pin bump the GitOps tick lands.

The pin is invisible in the rendered object's behaviour, which is what makes it drift-prone: a
new role copied from a sibling that predates this test loses the pin, the manifest still
schema-checks, the deploy is green, and the ReplicaSet count climbs again with no signal. The
API server has no way to enforce a repo-wide default, so this test is the enforcement.

Rollback depth is not the reason to raise the number back. Rollbacks here go through
`git revert` + a redeploy, not `kubectl rollout undo`, so history beyond the handful needed to
read a recent rollout is not load-bearing.

The scan is textual rather than a YAML parse: these are Jinja templates, and rendering them
needs the full inventory (see the sibling test_volume_names_descriptive.py for the same call).

Run: uv run pytest ansible/tests/k8s/test_deployment_revision_history_limit.py
"""

import pytest

from pathlib import Path
from _helpers import K8S_ROLES


LIMIT = "revisionHistoryLimit: 3"


def _deployment_specs(text: str) -> list[list[str]]:
    """Return the top-level `spec:` body of each Deployment document in a template.

    A template may hold several documents (karakeep, claude-otel), and only the Deployment
    ones are in scope — a DaemonSet has the same field but no ReplicaSets to accumulate, and
    a StatefulSet's history is bounded differently.
    """
    lines = text.split("\n")
    specs = []
    for i, line in enumerate(lines):
        if line.strip() != "kind: Deployment" or line.startswith(" "):
            continue
        # Walk to this document's column-0 `spec:`, stopping at the next document.
        j = i + 1
        while j < len(lines) and lines[j] != "spec:":
            if lines[j].startswith("---"):
                break
            j += 1
        if j >= len(lines) or lines[j] != "spec:":
            continue
        # The spec body runs until the next column-0 key or document separator.
        body = []
        for cur in lines[j + 1 :]:
            if cur and not cur.startswith(" ") and not cur.startswith("#"):
                break
            body.append(cur)
        specs.append(body)
    return specs


def _templates() -> list[Path]:
    return sorted(
        p
        for p in K8S_ROLES.glob("*/templates/*.j2")
        if "kind: Deployment" in p.read_text()
    )


def test_the_scan_finds_the_deployment_templates():
    """A scan that matches nothing passes vacuously. Pin the population it is asserting over."""
    found = _templates()
    assert len(found) >= 55, (
        f"expected the repo's ~58 Deployment templates, found {len(found)}"
    )


@pytest.mark.parametrize(
    "template", _templates(), ids=lambda p: f"{p.parents[1].name}/{p.name}"
)
def test_every_deployment_pins_its_revision_history_limit(template):
    specs = _deployment_specs(template.read_text())
    assert specs, (
        f"{template} declares kind: Deployment but no top-level spec: was found"
    )
    for spec in specs:
        assert any(line.strip() == LIMIT for line in spec), (
            f"{template.relative_to(K8S_ROLES)} has a Deployment with no `{LIMIT}` in its "
            f"top-level spec. Unpinned, it keeps 10 dead ReplicaSets instead of 3."
        )


def test_the_guard_rejects_an_unpinned_deployment():
    """The red-proof half: a Deployment spec without the pin must fail the same predicate.

    Without this, a scan that silently stopped matching would be indistinguishable from a repo
    that is fully compliant — both report green.
    """
    unpinned = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\nspec:\n  replicas: 1\n"
    pinned = unpinned.replace("spec:\n", f"spec:\n  {LIMIT}\n", 1)

    (spec,) = _deployment_specs(unpinned)
    assert not any(line.strip() == LIMIT for line in spec)

    (spec,) = _deployment_specs(pinned)
    assert any(line.strip() == LIMIT for line in spec)


def test_the_guard_ignores_a_daemonset():
    """A DaemonSet owns no ReplicaSets, so it is out of scope and must not be scanned."""
    assert (
        _deployment_specs(
            "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: x\nspec:\n  x: 1\n"
        )
        == []
    )
