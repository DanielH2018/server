#!/usr/bin/env python3
"""tdarr pulls its image onto the node before the apply that triggers the Recreate (#3875).

tdarr's Deployment is `Recreate`, so the apply stops the old pod before the kubelet pulls the
new image. The 1432 MiB pull took 25m37s on 2026-10-08 (#3689), and tdarr served nothing for
that whole time. tasks/main.yml pulls the image with `k3s crictl` before it includes
k8s/manifests, which keeps the old pod serving through the pull.

A pull placed after the include saves nothing and still passes a deploy, so the order is what
these tests hold. The pull is local, so it helps only while media-data's PV pins tdarr to the
play host; the second test holds that coupling.

Under Flux the same pull is the `tdarr-prepull` Kustomization, which `flux_prepull_timeout`
opts tdarr into (D5 step 26). Its Job copies the Deployment's placement, so the third test
renders tdarr as a published tag and holds that the Job lands on the PV's node.

Run: uv run pytest ansible/tests/services/test_tdarr_prepulls_its_image.py
"""

from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from _helpers import K8S_ROLES, load_defaults, load_tasks
from _k8s_render import host_context, pod_spec, render_role_template
from renderer import homelab_render
from renderer.renderer_lib.bundle import CLUSTER_VARS_FILE
from renderer.renderer_lib.ordering import Ordering

_TDARR = K8S_ROLES / "tdarr"


def _cmd(task: dict) -> str:
    return str((task.get("ansible.builtin.command") or {}).get("cmd", ""))


def _when(task: dict) -> str:
    when = task.get("when", [])
    return " ".join(when) if isinstance(when, list) else str(when)


def test_the_image_is_pulled_before_the_manifests_apply_and_never_on_a_dry_run() -> (
    None
):
    tasks = load_tasks(_TDARR / "tasks" / "main.yml")
    pulls = [i for i, t in enumerate(tasks) if _cmd(t).startswith("k3s crictl pull ")]
    applies = [
        i
        for i, t in enumerate(tasks)
        if (t.get("ansible.builtin.include_role") or {}).get("name") == "k8s/manifests"
    ]
    assert len(pulls) == 1 and len(applies) == 1, (pulls, applies)
    pull = tasks[pulls[0]]
    assert pulls[0] < applies[0], (
        "the crictl pull runs after k8s/manifests applies the Deployment, so the Recreate has "
        "already stopped the old pod and tdarr is down for the whole pull again"
    )
    assert _cmd(pull) == "k3s crictl pull {{ tdarr_k8s_image }}", _cmd(pull)
    assert "not k8s_no_mutate" in _when(pull), (
        "a dry run would spend 25 minutes pulling"
    )
    assert pull.get("tags") == ["deploy"], "the pull must run whenever the apply does"


def test_the_local_pull_lands_on_the_node_the_media_volume_pins() -> None:
    # The pull runs on the play host. It reaches tdarr's node only because media-volume pins
    # the local PV to the same `inventory_hostname`, on the claim tdarr mounts.
    # Rendered at a sentinel host, so a PV pinned to a literal name or to any other variable
    # renders something other than the sentinel and fails here.
    pv = yaml_fast.safe_load(
        render_role_template(
            "media-volume", "pv.yaml.j2", {"inventory_hostname": "sentinel-node"}
        )
    )
    terms = pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]
    assert [v for t in terms for e in t["matchExpressions"] for v in e["values"]] == [
        "sentinel-node"
    ], "media-volume no longer pins the PV to the play host"
    assert (
        pv["spec"]["claimRef"]["name"] == load_defaults(_TDARR)["tdarr_k8s_media_claim"]
    )
    tasks = load_tasks(_TDARR / "tasks" / "main.yml")
    asserts = [t for t in tasks if "ansible.builtin.assert" in t]
    assert any(
        "[inventory_hostname]" in str(t["ansible.builtin.assert"]["that"])
        for t in asserts
    ), "nothing refuses a pull onto a node tdarr cannot run on"


def test_under_flux_the_prepull_job_pulls_tdarrs_image_onto_the_media_node(
    tmp_path,
) -> None:
    # Rendered through the real CLI with only tdarr published, as the renderer's own prepull
    # tests render code-server. Without the Deployment's node pin the render raises instead.
    cluster_file = tmp_path / CLUSTER_VARS_FILE
    cluster_file.write_text("")
    out = tmp_path / "out"
    code = homelab_render.cluster(
        out,
        ["tdarr"],
        None,
        flux_owned=lambda: {"tdarr"},
        cluster_file=cluster_file,
        ordering=lambda o: Ordering(flux_owned=frozenset(o)),
    )
    assert code == RENDER_OK
    flux = {
        d["metadata"]["name"]: d["spec"]
        for d in yaml_fast.safe_load_all((out / "flux" / "tdarr.yaml").read_text())
    }
    assert flux["tdarr-prepull"]["timeout"] == "3000s"
    assert {"name": "tdarr-prepull"} in flux["tdarr"]["dependsOn"]
    [job] = yaml_fast.safe_load_all(
        (out / "cluster" / "tdarr-prepull" / "job.yaml").read_text()
    )
    [deployment] = [
        d
        for d in yaml_fast.safe_load_all(
            (out / "cluster" / "tdarr" / "deployment.yaml").read_text()
        )
        if d.get("kind") == "Deployment"
    ]
    # media-volume pins the PV to its play host (the test above), which is the host whose
    # containers_list deploys it.
    media_host = "daniel-box"
    assert "media-volume" in [
        e["name"] for e in host_context(media_host)["containers_list"]
    ]
    job_terms = pod_spec(job)["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert [
        v for t in job_terms for e in t["matchExpressions"] for v in e["values"]
    ] == [media_host], (
        "the pre-pull lands on a node other than the one media-data pins tdarr to"
    )
    assert pod_spec(job)["affinity"] == pod_spec(deployment)["affinity"]
    assert [c["image"] for c in pod_spec(job)["containers"]] == [
        load_defaults(_TDARR)["tdarr_k8s_image"]
    ]
