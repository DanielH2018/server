"""The `longhorn-restore-drill` tag renders an empty stamp ConfigMap that Flux creates once and keeps (H2.3b).

The drill's rotation, retry and seen state moves into `longhorn-restore-drill-stamps` (H2.3c),
and no render can restore it. Two annotations protect it. `ssa: IfNotPresent` stops Flux
reverting the drill's writes. `prune: disabled` keeps the object when a revert or a render bug
drops it from the tag, which prunes like any per-tag Kustomization.

Run: uv run pytest ansible/tests/k8s/test_restore_drill_stamps.py
"""

import bundle_census
from _helpers import load_defaults
from _k8s_render import render_role_template
from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST_VARS
from lib.k8s_roles import tag_owners
from lib.repo_paths import K3S_ROLE, K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.bundle import kustomization_text

ROLE = K8S_ROLES / "longhorn-restore-drill"
TAG = "longhorn-restore-drill"


def _stamps() -> dict:
    (doc,) = yaml_fast.safe_load_all(
        render_role_template(TAG, "stamps-configmap.yaml.j2")
    )
    return doc


def test_the_stamp_configmap_renders_empty_with_both_annotations():
    doc = _stamps()
    assert doc["kind"] == "ConfigMap"
    assert doc["metadata"]["name"] == "longhorn-restore-drill-stamps"
    assert doc["metadata"]["annotations"] == {
        "kustomize.toolkit.fluxcd.io/ssa": "IfNotPresent",
        "kustomize.toolkit.fluxcd.io/prune": "disabled",
    }
    assert "data" not in doc and "binaryData" not in doc


def test_the_stamp_namespace_equals_the_host_drills():
    """A mirrored pair: the host drill still reads the `setup/k3s` key until H2.3f."""
    drill = load_defaults(ROLE)["longhorn_drill_namespace"]
    host = load_defaults(K3S_ROLE)["k3s_longhorn_restore_drill_namespace"]
    assert drill == host == _stamps()["metadata"]["namespace"]


def test_the_tag_is_flux_owned_and_waits_on_longhorn():
    entry = next(
        e
        for e in yaml_fast.safe_load(K8S_HOST_VARS.read_text())["containers_list"]
        if e["name"] == TAG
    )
    assert tag_owners(K8S_HOST_VARS)[TAG] == "flux"
    # Any further field widens the entry's readers beyond the four the landing expects.
    assert entry == {
        "name": TAG,
        "platform": "k8s",
        "owner": "flux",
        "depends_on": ["longhorn"],
    }


def test_the_census_finds_the_stamp_in_the_real_render_and_flags_its_loss(tmp_path):
    out = tmp_path / "out"
    assert homelab_render.cluster(out, [TAG], None) == 0
    assert bundle_census.member_census(out, [TAG]) == []
    # Red case: a render whose kustomization no longer lists the ConfigMap.
    (out / "cluster" / TAG / "kustomization.yaml").write_text(kustomization_text([]))
    assert bundle_census.member_census(out, [TAG]) == [
        f"{TAG} renders no ConfigMap longhorn-restore-drill-stamps, a named member: a "
        "plumbing tag that drops it leaves it serving with nothing managing it"
    ]
