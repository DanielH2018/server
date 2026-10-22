#!/usr/bin/env python3
"""home-assistant, valheim and jellyfin pull their images before the apply that recreates them (#5855).

Each runs a `Recreate` Deployment on a large image from GHCR: 720.85 MB, 369.78 MB and
340.68 MB compressed on 2026-10-22. A digest bump stops the old pod before the kubelet pulls
the new image, so without a pre-pull each is down for the whole pull, the shape that took
karakeep down on 2026-10-19 (#5778). `flux_prepull_timeout` in each role's defaults adds a
`<tag>-prepull` Kustomization that `<tag>` depends on, so the old pod serves through the pull.

The test renders each role as the one published tag, as the tdarr test does, and holds the
issue's verify-by: the Kustomization exists, `<tag>` depends on it, and its Job pulls every
image the Deployment runs onto the node the Deployment names. jellyfin names its node only so
the pre-pull can copy it; without that pin the render raises.

Run: uv run pytest ansible/tests/services/test_recreate_media_and_game_roles_prepull.py
"""

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from _helpers import K8S_ROLES, load_defaults
from _k8s_render import pod_spec
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.ordering import Ordering

TAGS = ("home-assistant", "valheim", "jellyfin")


def _docs(path) -> list[dict]:
    return [d for d in yaml_fast.safe_load_all(path.read_text()) if d]


@pytest.mark.parametrize("tag", TAGS)
def test_the_role_prepulls_its_images_before_it_applies(tag, tmp_path) -> None:
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text("")
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [tag],
        None,
        flux_owned=lambda: {tag},
        cluster_file=cluster_file,
        ordering=lambda o: Ordering(flux_owned=frozenset(o)),
    )
    assert code == RENDER_OK
    flux = {
        d["metadata"]["name"]: d["spec"] for d in _docs(out / "flux" / f"{tag}.yaml")
    }
    prepull = f"{tag}-prepull"
    assert (
        flux[prepull]["timeout"]
        == load_defaults(K8S_ROLES / tag)["flux_prepull_timeout"]
    )
    assert {"name": prepull} in flux[tag]["dependsOn"]
    [job] = _docs(out / "cluster" / prepull / "job.yaml")
    [deployment] = [
        d
        for d in _docs(out / "cluster" / tag / "deployment.yaml")
        if d["kind"] == "Deployment"
    ]
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = pod_spec(deployment)
    images = {c["image"] for c in pod["containers"] + pod.get("initContainers", [])}
    assert {c["image"] for c in pod_spec(job)["containers"]} == images
    for key in ("nodeSelector", "affinity"):
        assert pod_spec(job).get(key) == pod.get(key)
