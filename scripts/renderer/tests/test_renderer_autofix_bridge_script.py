"""autofix-bridge's script ConfigMap is rendered, and its reader's `checksum/restart` covers it.

D5's imperative-ConfigMap row: the ConfigMap used to be staged on the node and applied by a
task, outside the render, so under Flux no `checksum/restart` could see a module edit. These
tests render the tag as flux-owned through `homelab_render.cluster`, with a fixture `.source`
in `tmp_path` holding the keys the render references.

Run: uv run pytest scripts/renderer/tests/test_renderer_autofix_bridge_script.py
"""

import pytest
import yaml

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.repo_paths import ANSIBLE
from renderer import homelab_render
from renderer.renderer_lib.placeholders import references
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    pod_references,
    pod_template,
    restart_values,
)

TAG = "autofix-bridge"
CONFIG_MAP = "autofix-bridge-script"
CONFIG_MAP_FILE = "script-configmap.yaml"
# The four modules the pod imports, each key with the file it must equal.
MODULES = {
    "autofix.py": ANSIBLE / "roles/k8s/autofix-bridge/files/autofix.py",
    "bridge_common.py": ANSIBLE / "roles/k8s/monitor-bridge/files/bridge/common.py",
    "kuma_push.py": ANSIBLE / "roles/setup/common/files/kuma_push.py",
    "jobs_metrics.py": ANSIBLE / "roles/k8s/crowdsec/files/jobs_metrics.py",
}
ALL = frozenset({"ConfigMap", "Secret"})


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
    tmp = tmp_path_factory.mktemp("autofix-bridge")
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


def _edit_a_module(files: dict[str, str]) -> dict[str, str]:
    """`files` with one line added to autofix.py inside the rendered ConfigMap."""
    (config_map,) = yaml_fast.safe_load_all(files[CONFIG_MAP_FILE])
    config_map["data"]["autofix.py"] += "# one edited line\n"
    return {**files, CONFIG_MAP_FILE: yaml.safe_dump(config_map)}


def test_the_config_map_carries_every_module_byte_for_byte(rendered):
    files, _ = rendered
    (config_map,) = yaml_fast.safe_load_all(files[CONFIG_MAP_FILE])
    assert config_map["metadata"]["name"] == CONFIG_MAP
    assert config_map["data"] == {
        key: path.read_text() for key, path in MODULES.items()
    }


def test_the_deployment_mounts_a_config_map_its_own_render_holds(rendered):
    """The pod named this ConfigMap before the conversion too; what is new is that the
    tag's render holds it, which is all `checksum/restart` can hash."""
    files, _ = rendered
    template = pod_template(_deployment(files))
    assert template is not None
    assert ("ConfigMap", CONFIG_MAP) in pod_references(template)
    held = {
        (obj.get("kind"), (obj.get("metadata") or {}).get("name"))
        for text in files.values()
        for obj in yaml_fast.safe_load_all(text)
        if isinstance(obj, dict)
    }
    assert ("ConfigMap", CONFIG_MAP) in held


def test_the_stamped_checksum_covers_the_config_map(rendered):
    """The renderer's stamp is `_value` of the render, and differs from the value of the same
    render without the ConfigMap, the shape it had while a task applied it."""
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
    unmounted = {
        **files,
        "deployment.yaml": files["deployment.yaml"].replace(
            f"name: {CONFIG_MAP}\n", f"name: {CONFIG_MAP}-staged\n"
        ),
    }
    assert unmounted["deployment.yaml"] != files["deployment.yaml"]
    assert _value(_edit_a_module(unmounted), keys) == _value(unmounted, keys)
