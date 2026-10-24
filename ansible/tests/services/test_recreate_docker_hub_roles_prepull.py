#!/usr/bin/env python3
"""uptime-kuma, crowdsec and Grafana pull their images before the apply that recreates them (#5867).

Each runs a `Recreate` Deployment on a large Docker Hub image: louislam/uptime-kuma's 602.3 MB
and crowdsec's alpine/k8s at 320.8 MB, compressed amd64 sizes on 2026-10-22. A digest bump
stops the old pod before the kubelet pulls the new image, so without a pre-pull the alerting
spine or the LAPI is down for the whole pull. `flux_prepull_timeout` in each role's defaults
adds a `<tag>-prepull` Kustomization that `<tag>` depends on, so the old pod serves through it.

The test renders each role as the one published tag, as the tdarr test does, and holds the
issue's verify-by: the Kustomization exists, `<tag>` depends on it, and its Job pulls every
image the Deployment runs under the Deployment's own placement. observability's Job pulls for
Grafana alone, because the role's other workloads pin no node and three carry no `sh` (#5875).

Run: uv run pytest ansible/tests/services/test_recreate_docker_hub_roles_prepull.py
"""

import pytest

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from _helpers import K8S_ROLES, load_defaults
from _k8s_render import pod_spec
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.ordering import Ordering

TAGS = ("uptime-kuma", "crowdsec")


def _docs(path) -> list[dict]:
    return [d for d in yaml_fast.safe_load_all(path.read_text()) if d]


@pytest.mark.parametrize("tag", TAGS)
def test_the_role_prepulls_its_images_before_it_applies(tag, tmp_path) -> None:
    _assert_prepulls(tag, "deployment.yaml", tag, tmp_path)


def test_observability_prepulls_grafana_alone(tmp_path) -> None:
    _assert_prepulls("observability", "grafana.yaml", "grafana", tmp_path)


def _assert_prepulls(tag: str, file: str, workload: str, tmp_path) -> None:
    """`<tag>-prepull` exists, `<tag>` waits on it, and its one Job pulls `workload`'s images."""
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
    assert prepull in flux, f"the render carries no {prepull} Kustomization"
    assert (
        flux[prepull]["timeout"]
        == load_defaults(K8S_ROLES / tag)["flux_prepull_timeout"]
    )
    assert {"name": prepull} in flux[tag]["dependsOn"]
    [job] = _docs(out / "cluster" / prepull / "job.yaml")
    [deployment] = [
        d
        for d in _docs(out / "cluster" / tag / file)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == workload
    ]
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = pod_spec(deployment)
    images = {c["image"] for c in pod["containers"] + pod.get("initContainers", [])}
    assert {c["image"] for c in pod_spec(job)["containers"]} == images
    for key in ("nodeSelector", "affinity"):
        assert pod_spec(job).get(key) == pod.get(key)
