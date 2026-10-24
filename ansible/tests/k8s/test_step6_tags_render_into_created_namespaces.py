"""No tag of H1's bring-up step 6 renders an object into a namespace nothing creates by then (H2.3b).

Step 6 of the DR order waits until `cluster-base`, `metallb`, `coredns`, `longhorn` and
`longhorn-backup` read Ready, and nothing creates `homelab` until step 8. A `homelab` object in
one of them fails its apply with "namespace not found", the tag never reads Ready, and step 6
stalls with everything behind it. The restore drill's ConfigMap sits in `homelab` for that
reason, in a tag of its own that step 6 does not wait on (H2, *The drill's own tag*).

The guard renders each step-6 tag, collects the namespaced objects, and allows the namespaces
the five renders create plus k3s's own four.

Run: uv run pytest ansible/tests/k8s/test_step6_tags_render_into_created_namespaces.py
"""

from pathlib import Path

import pytest

from lib import yaml_fast
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_DIR, KUSTOMIZATION
from renderer.renderer_lib.ordering import TAG_KIND_SUFFIXES

# A literal set, so a tag added to or dropped from step 6 is a change to this file.
STEP_6_TAGS = frozenset(
    {"cluster-base", "metallb", "coredns", "longhorn", "longhorn-backup"}
)
K3S_NAMESPACES = frozenset({"default", "kube-system", "kube-public", "kube-node-lease"})
# H1's named member for `longhorn`: the guard must find it, or it read nothing.
LONGHORN_MEMBER = ("DaemonSet", "longhorn-manager", "longhorn-system")


def _objects(out: Path, tag: str) -> list[dict]:
    """The documents of `tag`'s directories the files their kustomizations list hold."""
    docs = []
    for suffix in ("", *TAG_KIND_SUFFIXES):
        directory = out / CLUSTER_DIR / f"{tag}{suffix}"
        if not (directory / KUSTOMIZATION).is_file():
            continue
        listing = yaml_fast.safe_load((directory / KUSTOMIZATION).read_text())
        for resource in listing.get("resources") or []:
            docs += [
                doc
                for doc in yaml_fast.safe_load_all((directory / resource).read_text())
                if isinstance(doc, dict)
            ]
    return docs


def stranded_objects(rendered: dict[str, list[dict]]) -> list[str]:
    """One line for each namespaced object whose namespace no step-6 render or k3s creates."""
    created = set(K3S_NAMESPACES)
    for docs in rendered.values():
        created |= {d["metadata"]["name"] for d in docs if d.get("kind") == "Namespace"}
    return [
        f"{tag} renders {doc['kind']} {doc['metadata']['name']} into namespace "
        f"{doc['metadata']['namespace']}, which nothing creates before step 8"
        for tag, docs in sorted(rendered.items())
        for doc in docs
        if doc["metadata"].get("namespace") not in (None, *created)
    ]


@pytest.fixture(scope="module")
def step_6_render(tmp_path_factory) -> dict[str, list[dict]]:
    out = tmp_path_factory.mktemp("step6") / "out"
    assert homelab_render.cluster(out, sorted(STEP_6_TAGS), None) == 0
    return {tag: _objects(out, tag) for tag in sorted(STEP_6_TAGS)}


def test_the_real_step_6_renders_leave_no_object_in_an_uncreated_namespace(
    step_6_render,
):
    assert stranded_objects(step_6_render) == []


def test_the_guard_read_all_five_tags_and_finds_longhorn_manager(step_6_render):
    """Non-vacuity: an empty render of a tag would pass the check above."""
    assert set(step_6_render) == STEP_6_TAGS
    assert all(step_6_render[tag] for tag in STEP_6_TAGS)
    found = {
        (d["kind"], d["metadata"]["name"], d["metadata"].get("namespace"))
        for d in step_6_render["longhorn"]
    }
    assert LONGHORN_MEMBER in found


def test_a_homelab_configmap_in_the_longhorn_render_is_flagged(step_6_render):
    """The red fixture of H2.3b: the drill's stamp ConfigMap put in `longhorn` by mistake."""
    stamp = {
        "kind": "ConfigMap",
        "metadata": {"name": "longhorn-restore-drill-stamps", "namespace": "homelab"},
    }
    rendered = {**step_6_render, "longhorn": [*step_6_render["longhorn"], stamp]}
    assert stranded_objects(rendered) == [
        "longhorn renders ConfigMap longhorn-restore-drill-stamps into namespace "
        "homelab, which nothing creates before step 8"
    ]
