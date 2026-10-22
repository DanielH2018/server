#!/usr/bin/env python3
"""karakeep's images are on its node before the apply that triggers the Recreate (#5778).

`karakeep` and `karakeep-meilisearch` are `Recreate`, so the apply stops the old pod before the
kubelet pulls the new image. On 2026-10-19 a digest bump of the 511 MB app image pulled from
GHCR for 16.5 minutes, failed on a corrupt layer, and karakeep served nothing meanwhile.
`flux_prepull_timeout` opts karakeep into the `karakeep-prepull` Kustomization, which pulls
every image the role's Deployments run, and the `karakeep` Kustomization depends on it.

Run: uv run pytest ansible/tests/services/test_karakeep_prepulls_its_images.py
"""

import pytest
from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from _helpers import K8S_ROLES, load_defaults
from _k8s_render import pod_spec
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.ordering import Ordering

TAG = "karakeep"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    # Rendered through the real CLI with only karakeep published, as the tdarr test does.
    tmp_path = tmp_path_factory.mktemp("karakeep-prepull")
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text("")
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        [TAG],
        None,
        flux_owned=lambda: {TAG},
        cluster_file=cluster_file,
        ordering=lambda o: Ordering(flux_owned=frozenset(o)),
    )
    assert code == RENDER_OK
    return out


def _deployments(out) -> list[dict]:
    return [
        doc
        for path in sorted((out / "cluster" / TAG).glob("*.yaml"))
        for doc in yaml_fast.safe_load_all(path.read_text())
        if isinstance(doc, dict) and doc.get("kind") == "Deployment"
    ]


def test_karakeep_waits_on_its_prepull_for_the_roles_timeout(rendered) -> None:
    flux = {
        d["metadata"]["name"]: d["spec"]
        for d in yaml_fast.safe_load_all(
            (rendered / "flux" / f"{TAG}.yaml").read_text()
        )
    }
    assert (
        flux[f"{TAG}-prepull"]["timeout"]
        == load_defaults(K8S_ROLES / TAG)["flux_prepull_timeout"]
    )
    assert {"name": f"{TAG}-prepull"} in flux[TAG]["dependsOn"]


def test_the_prepull_job_pulls_every_image_karakeeps_deployments_run(rendered) -> None:
    deployments = _deployments(rendered)
    # The two Recreate Deployments the #5778 outage came from, so a render that lost one
    # cannot pass by comparing an empty set with an empty set.
    names = {d["metadata"]["name"] for d in deployments}
    assert {"karakeep", "karakeep-meilisearch"} <= names
    wanted = {
        c["image"]
        for d in deployments
        for c in pod_spec(d).get("initContainers", []) + pod_spec(d)["containers"]
    }
    [job] = yaml_fast.safe_load_all(
        (rendered / "cluster" / f"{TAG}-prepull" / "job.yaml").read_text()
    )
    pulled = {c["image"] for c in pod_spec(job)["containers"]}
    assert pulled == wanted
    defaults = load_defaults(K8S_ROLES / TAG)
    assert {
        defaults["karakeep_k8s_image"],
        defaults["karakeep_k8s_chrome_image"],
    } <= pulled
    # Every Deployment prefers the same node, so the one Job lands where the pods will.
    assert all(
        pod_spec(job)["affinity"] == pod_spec(d)["affinity"] for d in deployments
    )
