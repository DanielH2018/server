"""monitor-bridge's script ConfigMaps are rendered, and its pod's `checksum/restart` covers them.

D5's imperative-ConfigMap row (#5536): the ConfigMap used to be staged on the node and applied
by a task, outside the render, so under Flux no `checksum/restart` could see a module edit.
The render now holds one ConfigMap per module, projected into the pod's /app. These tests
render the tag as flux-owned through `homelab_render.cluster`, with a fixture `.source` in
`tmp_path` holding the keys the render references.

Run: uv run pytest scripts/renderer/tests/test_renderer_monitor_bridge_script.py
"""

import json

import pytest
import yaml

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.placeholders import references
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    pod_references,
    pod_template,
    restart_values,
)

TAG = "monitor-bridge"
ROLE = K8S_ROLES / TAG
CONFIG_MAP_FILE = "script-configmap.yaml"
ALL = frozenset({"ConfigMap", "Secret"})
# k8s/manifests applies client-side, which copies each object into a
# `last-applied-configuration` annotation capped at 262144 bytes. Half of it leaves room for a
# module to grow before the apply is refused, the budget game-stats' ConfigMaps keep too.
ANNOTATION_CAP = 262144
ANNOTATION_BUDGET = ANNOTATION_CAP // 2


def _modules() -> list[str]:
    return yaml_fast.safe_load((ROLE / "defaults/main.yml").read_text())[
        "monitor_bridge_modules"
    ]


def _files(out) -> dict[str, str]:
    return {
        p.name: p.read_text() for p in sorted((out / "cluster" / TAG).glob("*.yaml"))
    }


def _render(out, owned, source_file, cluster_file):
    return homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: owned,
        cluster_file=cluster_file,
        source_file=source_file,
    )


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """The tag's flux-owned render, and the secret keys it references."""
    tmp = tmp_path_factory.mktemp("monitor-bridge")
    cluster_file = tmp / "cluster-vars.sops.env"
    cluster_file.write_text("k=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n")
    # A first render, unowned, names the keys the fixture `.source` must record.
    source_file = tmp / "cluster-vars.source"
    source_file.write_text("")
    assert _render(tmp / "unowned", set(), source_file, cluster_file) == RENDER_OK
    keys = sorted(
        {key for text in _files(tmp / "unowned").values() for key in references(text)}
    )
    source_file.write_text("".join(f"{key} {'d' * 64}\n" for key in keys))
    assert _render(tmp / "owned", {TAG}, source_file, cluster_file) == RENDER_OK
    return _files(tmp / "owned"), keys


def _config_maps(files: dict[str, str]) -> list[dict]:
    return [obj for obj in yaml_fast.safe_load_all(files[CONFIG_MAP_FILE]) if obj]


def _deployment(files: dict[str, str]) -> dict:
    (deployment,) = [
        obj
        for text in files.values()
        for obj in yaml_fast.safe_load_all(text)
        if isinstance(obj, dict) and obj.get("kind") == "Deployment"
    ]
    return deployment


def _value(files: dict[str, str], keys) -> str:
    source = {key: "d" * 64 for key in keys}
    return restart_values(TAG, {TAG: files}, {TAG: ALL}, keys, source)[TAG]


def _edit_a_module(files: dict[str, str], key: str = "checks_b2.py") -> dict[str, str]:
    """`files` with one line added to the module under `key` inside its rendered ConfigMap."""
    config_maps = _config_maps(files)
    (edited,) = [cm for cm in config_maps if key in cm["data"]]
    edited["data"][key] += "# one edited line\n"
    return {**files, CONFIG_MAP_FILE: yaml.safe_dump_all(config_maps)}


def test_each_module_ships_in_its_own_config_map_byte_for_byte(rendered):
    files, _ = rendered
    shipped = {}
    for config_map in _config_maps(files):
        assert config_map["kind"] == "ConfigMap"
        assert len(config_map["data"]) == 1, config_map["metadata"]["name"]
        shipped.update(config_map["data"])
    assert shipped == {
        module.replace("/", "_"): (ROLE / "files" / module).read_text()
        for module in _modules()
    }


def test_the_projected_volume_names_exactly_the_rendered_config_maps(rendered):
    """The name derivation sits in two templates; a drift between them is a pod that never
    starts, because a projected source naming an absent ConfigMap blocks the mount."""
    files, _ = rendered
    template = pod_template(_deployment(files))
    assert template is not None
    mounted = {name for kind, name in pod_references(template) if kind == "ConfigMap"}
    held = {cm["metadata"]["name"] for cm in _config_maps(files)}
    assert len(held) == len(_modules())
    assert mounted == held
    assert "monitor-bridge-script-checks-b2" in held


def test_every_config_map_stays_under_the_annotation_budget(rendered):
    files, _ = rendered
    sizes = {cm["metadata"]["name"]: len(json.dumps(cm)) for cm in _config_maps(files)}
    over = {name: size for name, size in sizes.items() if size > ANNOTATION_BUDGET}
    assert not over, (
        f"{over} exceed {ANNOTATION_BUDGET} bytes as JSON; split the module, which the 600-line "
        "rule in this role's CLAUDE.md asks for anyway"
    )


def test_one_config_map_for_every_module_would_be_refused_is_flagged(rendered):
    """The red case for the budget: the pre-conversion shape, every module in one object.

    Its client-side apply would store more than the annotation cap, which is why the render
    ships one ConfigMap per module.
    """
    files, _ = rendered
    merged = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "monitor-bridge-script", "namespace": "homelab"},
        "data": {k: v for cm in _config_maps(files) for k, v in cm["data"].items()},
    }
    assert len(json.dumps(merged)) > ANNOTATION_CAP


def test_the_stamped_checksum_covers_the_config_maps(rendered):
    """The renderer's stamp is `_value` of the render, and differs from the value of the same
    render without the ConfigMaps, the shape it had while a task applied them."""
    files, keys = rendered
    annotations = _deployment(files)["spec"]["template"]["metadata"]["annotations"]
    without = {name: text for name, text in files.items() if name != CONFIG_MAP_FILE}
    assert annotations[RESTART_ANNOTATION] == _value(files, keys)
    assert annotations[RESTART_ANNOTATION] != _value(without, keys)


def test_a_module_edit_moves_the_checksum_is_clean(rendered):
    files, keys = rendered
    assert _value(_edit_a_module(files), keys) != _value(files, keys)


def test_a_module_edit_the_deployment_does_not_mount_is_flagged(rendered):
    """The red fixture: a pod naming another ConfigMap, as when this one sat outside the render.

    The edit then leaves the checksum where it was, which is the defect the conversion fixes.
    """
    files, keys = rendered
    staged = "name: monitor-bridge-script-checks-b2\n"
    unmounted = {
        **files,
        "deployment.yaml": files["deployment.yaml"].replace(
            staged, staged.replace("\n", "-staged\n")
        ),
    }
    assert unmounted["deployment.yaml"] != files["deployment.yaml"]
    assert _value(_edit_a_module(unmounted), keys) == _value(unmounted, keys)
