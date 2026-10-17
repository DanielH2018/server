"""game-stats' rendered script ConfigMaps and the `checksum/restart` that covers them (#5444).

Each game's pod runs a script from a ConfigMap the role renders with `lookup('file')`. The
script imports `stats_lib`, so the ConfigMap must carry both keys, and an edit to either file
must move the `checksum/restart` of every pod that loads it. The render is a real placeholder
render of game-stats as a flux-owned tag, made once per module. The edits are applied to that
render's ConfigMaps and re-hashed with `restart_values`, the function the renderer stamps from.

Run: uv run pytest scripts/renderer/tests/test_renderer_game_stats_restart.py
"""

import ast
import json

import pytest
import yaml

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.repo_paths import K8S_ROLES
from renderer import homelab_render
from renderer.renderer_lib.restart import (
    RESTART_ANNOTATION,
    any_pod_template,
    restart_values,
)

TAG = "game-stats"
FILES = K8S_ROLES / TAG / "files"
LIB = "stats_lib.py"
# Each game's pod, the ConfigMap it mounts, and the entry script its command runs.
GAMES = {
    "terraria-stats": ("terraria-stats-script", "stats.py"),
    "valheim-stats": ("valheim-stats-script", "valheim_stats.py"),
}
KINDS = frozenset({"ConfigMap", "Secret"})
# k8s/manifests applies client-side, which copies the object into an annotation capped at
# 262144 bytes. Half of it leaves room for the scripts to grow before the apply is refused.
ANNOTATION_BUDGET = 262144 // 2


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict[str, str]:
    """game-stats' flux-owned placeholder render, each file name mapped to its text."""
    root = tmp_path_factory.mktemp("game-stats")
    source = root / "cluster-vars.source"
    source.write_text("")
    cluster_file = root / "cluster-vars.sops.env"
    cluster_file.write_text("k=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n")
    out = root / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: {TAG},
        cluster_file=cluster_file,
        source_file=source,
    )
    assert code == RENDER_OK
    return {
        p.name: p.read_text() for p in sorted((out / "cluster" / TAG).glob("*.yaml"))
    }


def _objects(files: dict[str, str]) -> dict[tuple[str, str], dict]:
    return {
        (obj["kind"], obj["metadata"]["name"]): obj
        for text in files.values()
        for obj in yaml_fast.safe_load_all(text)
        if obj and "metadata" in obj  # kustomization.yaml names no object
    }


def _pod(obj: dict) -> dict:
    template = any_pod_template(obj)
    assert template is not None, obj["metadata"]["name"]
    return template


def _values(files: dict[str, str]) -> dict[str, str]:
    targets = dict.fromkeys(GAMES, KINDS)
    return restart_values(TAG, {TAG: files}, targets, (), {})


def _with_edit(files: dict[str, str], key: str) -> dict[str, str]:
    """`files` with a line appended to `key` in every ConfigMap that carries it."""
    edited = {}
    for name, text in files.items():
        docs = [obj for obj in yaml_fast.safe_load_all(text) if obj]
        for obj in docs:
            if obj["kind"] == "ConfigMap" and key in obj.get("data", {}):
                obj["data"][key] += "# edited\n"
        edited[name] = yaml.safe_dump_all(docs)
    return edited


def _without_config_maps(files: dict[str, str]) -> dict[str, str]:
    """`files` as the imperative shape had them: the script ConfigMaps outside the render."""
    return {
        name: yaml.safe_dump_all(
            [o for o in yaml_fast.safe_load_all(text) if o and o["kind"] != "ConfigMap"]
        )
        for name, text in files.items()
    }


def local_imports_missing(entry: str, data: dict[str, str]) -> set[str]:
    """The role's own modules that `entry` imports and the ConfigMap `data` does not carry."""
    own = {p.stem for p in FILES.glob("*.py")}
    imported = set()
    for node in ast.walk(ast.parse(data[entry])):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return {f"{name}.py" for name in imported & own} - set(data)


@pytest.mark.parametrize("pod", sorted(GAMES))
def test_each_config_map_carries_the_script_its_pod_runs_and_its_imports(rendered, pod):
    objects = _objects(rendered)
    config_map, entry = GAMES[pod]
    data = objects[("ConfigMap", config_map)]["data"]
    for key in (entry, LIB):
        assert data[key].rstrip("\n") == (FILES / key).read_text().rstrip("\n"), key
    assert local_imports_missing(entry, data) == set()
    assert len(json.dumps(objects[("ConfigMap", config_map)])) < ANNOTATION_BUDGET
    template = _pod(objects[("Deployment", pod)])
    volumes = {
        v["name"]: v.get("configMap", {}).get("name")
        for v in template["spec"]["volumes"]
    }
    container = template["spec"]["containers"][0]
    mount = next(
        m["mountPath"]
        for m in container["volumeMounts"]
        if volumes.get(m["name"]) == config_map
    )
    assert container["command"][-1] == f"{mount}/{entry}"


def test_a_config_map_without_stats_lib_is_flagged(rendered):
    data = dict(_objects(rendered)[("ConfigMap", "terraria-stats-script")]["data"])
    del data[LIB]
    assert local_imports_missing("stats.py", data) == {LIB}


def test_both_pods_carry_the_restart_checksum_the_renderer_derives(rendered):
    stamped = {
        obj["metadata"]["name"]: _pod(obj)["metadata"]["annotations"].get(
            RESTART_ANNOTATION
        )
        for (kind, _), obj in _objects(rendered).items()
        if kind == "Deployment"
    }
    assert stamped == _values(rendered)
    assert set(stamped) == set(GAMES) and all(stamped.values())


def test_a_stats_lib_edit_moves_both_pods_checksums(rendered):
    before, after = _values(rendered), _values(_with_edit(rendered, LIB))
    assert all(after[pod] != before[pod] for pod in GAMES)


@pytest.mark.parametrize("pod", sorted(GAMES))
def test_an_entry_script_edit_moves_only_its_own_pods_checksum(rendered, pod):
    before = _values(rendered)
    after = _values(_with_edit(rendered, GAMES[pod][1]))
    assert after[pod] != before[pod]
    assert all(after[other] == before[other] for other in GAMES if other != pod)


def test_a_config_map_outside_the_render_moves_no_checksum(rendered):
    """The red case: the imperative shape leaves the pods nothing to hash."""
    assert _values(_without_config_maps(rendered)) == {}
