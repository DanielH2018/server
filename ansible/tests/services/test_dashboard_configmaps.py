"""observability's dashboard ConfigMaps are a render of the boards the role lists, ready for Flux.

`templates/grafana-dashboards.yaml.j2` renders one ConfigMap per `observability_dashboard_folders`
entry, holding the boards that entry lists, each passed through `grafana_deploy_annotated`. The
plan is docs/target-architecture/deploy/d5-bulk-move.md, the imperative-ConfigMap row: the
ConfigMaps become a render, so observability's flip can hand them to Flux.

- The lists must match the files. A board on disk that no list names reaches no Grafana, and a
  listed board with no file fails the render.
- Each rendered key is the filter's output for its file, so both deploy-annotation queries
  reach every board.
- The file stays out of the role's Flux bundle while Ansible owns observability, and a copy with
  the flip's one-line edit renders the same ConfigMaps into it, each under the API server's
  1 MiB object cap.

Run: uv run pytest ansible/tests/services/test_dashboard_configmaps.py
"""

import json
import shutil
from pathlib import Path

import pytest

from _helpers import ANSIBLE, load_defaults
from _k8s_render import rendered_texts
from grafana_dashboards import grafana_deploy_annotated
from lib import yaml_fast
from renderer import shadow_diff
from renderer.renderer_lib import cluster
from renderer.renderer_lib.context import load_stubs

ROLE = ANSIBLE / "roles" / "k8s" / "observability"
DASHBOARDS_DIR = ROLE / "files" / "dashboards"
TEMPLATE = "grafana-dashboards.yaml.j2"
RENDERED = "grafana-dashboards.yaml"
# The API server refuses an object larger than this.
OBJECT_CAP = 1024 * 1024
# The biggest board, named so a census over the folders cannot pass empty.
LARGEST = ("Infrastructure", "node-exporter-full.json")


def list_drift(dashboards_dir: Path, listed: dict[str, list[str]]) -> list[str]:
    """One line per board on disk that no list names, and per listed board with no file."""
    on_disk = {(p.parent.name, p.name) for p in dashboards_dir.glob("*/*.json")}
    named = {(folder, board) for folder, boards in listed.items() for board in boards}
    return [
        f"{folder}/{board}: on disk, so add it under {folder} in observability_dashboard_folders"
        for folder, board in sorted(on_disk - named)
    ] + [
        f"{folder}/{board}: listed with no file"
        for folder, board in sorted(named - on_disk)
    ]


def _listed() -> dict[str, list[str]]:
    return load_defaults(ROLE)["observability_dashboard_folders"]


def test_every_board_on_disk_is_listed_and_every_listed_board_exists():
    assert LARGEST[1] in _listed()[LARGEST[0]]
    assert list_drift(DASHBOARDS_DIR, _listed()) == []


def test_an_unlisted_board_and_a_listed_board_with_no_file_are_flagged(tmp_path):
    (tmp_path / "Apps").mkdir()
    (tmp_path / "Apps" / "kept.json").write_text("{}")
    (tmp_path / "Apps" / "new.json").write_text("{}")
    assert list_drift(tmp_path, {"Apps": ["kept.json", "gone.json"]}) == [
        "Apps/new.json: on disk, so add it under Apps in observability_dashboard_folders",
        "Apps/gone.json: listed with no file",
    ]


def _configmaps(text: str) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc for doc in yaml_fast.safe_load_all(text) if doc
    }


@pytest.fixture(scope="module")
def rendered() -> dict[str, dict]:
    (text,) = [
        t
        for role, name, t in rendered_texts()
        if (role, name) == ("observability", TEMPLATE)
    ]
    return _configmaps(text)


def test_each_folder_renders_one_configmap_holding_its_annotated_boards(rendered):
    defaults = load_defaults(ROLE)
    assert sorted(rendered) == sorted(
        f"grafana-dashboards-{f.lower()}" for f in _listed()
    )
    for folder, boards in _listed().items():
        configmap = rendered[f"grafana-dashboards-{folder.lower()}"]
        assert configmap["metadata"]["namespace"] == "observability"
        assert sorted(configmap["data"]) == sorted(boards)
        for board in boards:
            expected = grafana_deploy_annotated(
                (DASHBOARDS_DIR / folder / board).read_text(),
                defaults["observability_loki_homelab_uid"],
                defaults["observability_deploy_annotation_expr"],
                defaults["observability_flux_deploy_annotation_expr"],
            )
            assert configmap["data"][board] == expected, f"{folder}/{board}"
            names = [e["name"] for e in json.loads(expected)["annotations"]["list"]]
            assert {"Deploys", "Flux deploys"} <= set(names), f"{folder}/{board}"


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    path = tmp_path_factory.mktemp("stubs") / "stubs.yml"
    assert shadow_diff.main(["stubs", "--out", str(path)]) == 0
    return load_stubs(path)


def test_the_bundle_leaves_the_configmaps_out_while_ansible_owns_observability(stubs):
    """Ansible's server-side apply in dashboards.yml owns them until the flip."""
    assert RENDERED not in cluster.render_tag("observability", stubs).files


def test_the_flips_edit_renders_the_configmaps_into_the_bundle_under_the_cap(
    stubs, rendered, tmp_path
):
    """Dropping the exclusion is all the flip needs: the same ConfigMaps reach `observability`."""
    shutil.copytree(ROLE, tmp_path / "observability")
    tasks = tmp_path / "observability" / "tasks" / "main.yml"
    text = tasks.read_text()
    assert f"[service.yaml, {RENDERED}]" in text
    tasks.write_text(text.replace(f"[service.yaml, {RENDERED}]", "[service.yaml]"))
    bundle = cluster.render_tag("observability", stubs, roles=tmp_path).files
    flipped = _configmaps(bundle[RENDERED])
    assert {name: doc["data"] for name, doc in flipped.items()} == {
        name: doc["data"] for name, doc in rendered.items()
    }
    sizes = {name: len(json.dumps(doc).encode()) for name, doc in flipped.items()}
    assert sizes["grafana-dashboards-infrastructure"] > 256 * 1024
    assert max(sizes.values()) < OBJECT_CAP, sizes
