"""A pre-pull role's `<tag>-prepull`: its Jobs, its edges and its timeout (#5525).

The fixture renders code-server, the real role that sets `flux_prepull_timeout`, through the
real CLI as if the inventory marked it `owner: flux`. code-server is not flux-owned on master,
so no live tag renders `<tag>-prepull` yet.

Run: uv run pytest scripts/renderer/tests/test_renderer_prepull.py
"""

import pytest
from _job_census import load, problems

from deploy_tools.bundle_census import prepull_tags, report
from lib import yaml_fast
from lib.exit_codes import OK, RENDER_OK
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.cluster import RenderedTag
from renderer.renderer_lib.environment import RenderError
from renderer.renderer_lib.ordering import Ordering, prepull_timeouts
from renderer.renderer_lib.prepull import attach_prepulls, prepull_files, targets

DOTENV = "peanut_password=ENC[AES256_GCM,data:eA==,iv:aQ==,tag:dA==,type:str]\n"
TAG = "code-server"


def _render(tmp_path, owned: set[str]):
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text(DOTENV)
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: owned,
        cluster_file=cluster_file,
        ordering=lambda o: Ordering(flux_owned=frozenset(o)),
    )
    assert code == RENDER_OK
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    return _render(tmp_path_factory.mktemp("prepull"), {TAG})


def _kustomizations(out) -> dict[str, dict]:
    docs = yaml_fast.safe_load_all((out / "flux" / f"{TAG}.yaml").read_text())
    return {doc["metadata"]["name"]: doc for doc in docs}


def _depends(doc: dict) -> list[str]:
    return [e["name"] for e in doc["spec"].get("dependsOn", [])]


def _pod_spec(path) -> dict:
    docs = yaml_fast.safe_load_all(path.read_text())
    return next(d for d in docs if d.get("kind") in ("Deployment", "Job"))["spec"][
        "template"
    ]["spec"]


def test_the_tag_waits_on_its_prepull_and_its_snapshot_side_by_side(rendered):
    docs = _kustomizations(rendered)
    assert list(docs) == [TAG, f"{TAG}-snapshot", f"{TAG}-prepull"]
    assert _depends(docs[TAG]) == ["cluster-vars", f"{TAG}-prepull", f"{TAG}-snapshot"]
    # Siblings, never a chain, so the critical path is the longer of the two.
    assert f"{TAG}-prepull" not in _depends(docs[f"{TAG}-snapshot"])
    assert f"{TAG}-snapshot" not in _depends(docs[f"{TAG}-prepull"])


def test_the_prepull_kustomization_takes_the_roles_prepull_timeout(rendered):
    docs = _kustomizations(rendered)
    assert docs[f"{TAG}-prepull"]["spec"]["timeout"] == "3600s"
    assert docs[f"{TAG}-snapshot"]["spec"]["timeout"] == "600s"


def test_the_job_pulls_the_deployments_ghcr_ref_onto_its_preferred_node(rendered):
    deployment = _pod_spec(rendered / "cluster" / TAG / "deployment.yaml")
    job = _pod_spec(rendered / "cluster" / f"{TAG}-prepull" / "job.yaml")
    assert job["nodeSelector"] == {"kubernetes.io/hostname": "daniel-box"}
    # The init container and the server run one image, so the Job pulls it once.
    [container] = job["containers"]
    assert container["image"] == deployment["containers"][0]["image"]
    assert container["image"].startswith("ghcr.io/")
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_the_prepull_directory_passes_the_job_census_and_check_bundle(rendered):
    assert problems(load(rendered)) == []
    assert report(rendered, [TAG]) == OK


def test_an_unpublished_prepull_role_renders_no_prepull_directory(tmp_path):
    out = _render(tmp_path, set())
    assert sorted(p.name for p in (out / "cluster").iterdir()) == [TAG]


def _deployment(spec: str) -> dict[str, str]:
    head = "kind: Deployment\nmetadata:\n  name: app\nspec:\n  template:\n    spec:\n"
    return {"deployment.yaml": head + spec}


PINNED = (
    "      affinity:\n        nodeAffinity:\n"
    "          requiredDuringSchedulingIgnoredDuringExecution:\n"
    "            nodeSelectorTerms:\n"
    "              - matchExpressions:\n"
    "                  - {key: kubernetes.io/hostname, operator: In, values: [a, b]}\n"
    "      initContainers:\n        - {name: init, image: 'app:1'}\n"
    "      containers:\n        - {name: app, image: 'app:1'}\n"
    "        - {name: side, image: 'side:2'}\n"
)


def test_every_named_node_gets_a_job_pulling_each_distinct_image():
    nodes, images = targets("app", _deployment(PINNED))
    assert (nodes, images) == (["a", "b"], ["app:1", "side:2"])
    jobs = yaml_fast.safe_load_all(prepull_files("app", nodes, images, 60)["job.yaml"])
    assert [j["spec"]["template"]["spec"]["nodeSelector"] for j in jobs] == [
        {"kubernetes.io/hostname": "a"},
        {"kubernetes.io/hostname": "b"},
    ]


def _job_name(image: str) -> str:
    text = prepull_files("app", ["a"], [image], 60)["job.yaml"]
    return yaml_fast.safe_load(text)["metadata"]["name"]


def test_the_job_name_moves_with_the_image_and_holds_without_it():
    assert _job_name("app:1") == _job_name("app:1")
    assert _job_name("app:1") != _job_name("app:2")


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            _deployment("      containers:\n        - {name: app, image: 'app:1'}\n"),
            "names a node",
        ),
        (
            {"configmap.yaml": "kind: ConfigMap\nmetadata:\n  name: x\n"},
            "no Deployment",
        ),
    ],
)
def test_a_render_the_prepull_cannot_place_fails(files, message):
    with pytest.raises(RenderError, match=message):
        targets("app", files)


def test_a_timeout_that_leaves_the_job_no_time_fails_the_render(tmp_path):
    defaults = tmp_path / "app" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text("flux_prepull_timeout: 60s\n")
    tag = RenderedTag("app", _deployment(PINNED))
    with pytest.raises(RenderError, match="leaves the pre-pull Job no time"):
        attach_prepulls([tag], ["app"], tmp_path)


def test_the_prepull_tags_are_the_roles_that_set_a_prepull_timeout():
    """Named members: code-server sets `flux_prepull_timeout`, and bento-pdf does not."""
    assert prepull_tags(["bento-pdf", "code-server"]) == ({"code-server"}, [])


@pytest.mark.parametrize("value", ['"{{ pull_budget }}"', "3600", '""'])
def test_a_prepull_timeout_that_is_not_a_literal_duration_is_a_problem(tmp_path, value):
    defaults = tmp_path / "app" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text(f"flux_prepull_timeout: {value}\n")
    found, problems = prepull_timeouts(["app"], tmp_path)
    assert found == {}
    assert len(problems) == 1 and problems[0].startswith("app's flux_prepull_timeout")
