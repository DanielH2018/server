#!/usr/bin/env python3
"""tdarr pulls its image onto the node before the apply that triggers the Recreate (#3875).

tdarr's Deployment is `Recreate`, so the apply stops the old pod before the kubelet pulls the
new image. The 1432 MiB pull took 25m37s on 2026-10-08 (#3689), and tdarr served nothing for
that whole time. tasks/main.yml pulls the image with `k3s crictl` before it includes
k8s/manifests, which keeps the old pod serving through the pull.

A pull placed after the include saves nothing and still passes a deploy, so the order is what
these tests hold. The pull is local, so it helps only while media-data's PV pins tdarr to the
play host; the second test holds that coupling.

Run: uv run pytest ansible/tests/services/test_tdarr_prepulls_its_image.py
"""

from _helpers import K8S_ROLES, load_defaults, load_tasks

_TDARR = K8S_ROLES / "tdarr"
_PV_TEMPLATE = K8S_ROLES / "media-volume" / "templates" / "pv.yaml.j2"


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
    pv = _PV_TEMPLATE.read_text()
    assert "{{ inventory_hostname }}" in pv, (
        "media-volume no longer pins the PV to the play host"
    )
    assert (
        load_defaults(_TDARR)["tdarr_k8s_media_claim"]
        == load_defaults(K8S_ROLES / "media-volume")["media_volume_claim"]
    )
    tasks = load_tasks(_TDARR / "tasks" / "main.yml")
    asserts = [t for t in tasks if "ansible.builtin.assert" in t]
    assert any(
        "[inventory_hostname]" in str(t["ansible.builtin.assert"]["that"])
        for t in asserts
    ), "nothing refuses a pull onto a node tdarr cannot run on"
