#!/usr/bin/env python3
"""tdarr's VAAPI encode runs as an initContainer, not as an exec from Ansible (#5451).

The encode proves tdarr's OWN ffmpeg can drive the render node. A Flux-applied Job cannot run
it, because the Job image has no ffmpeg and the Job census forbids spawning a process. So the
operator chose option 1 of #5451: an initContainer in tdarr's pod, with tdarr's image and the
same device request, so a failed encode keeps tdarr from starting under Ansible and Flux alike.

Run: uv run pytest ansible/tests/services/test_tdarr_vaapi_encode_init_container.py
"""

from lib import yaml_fast
from _helpers import K8S_ROLES, load_defaults, load_tasks
from _k8s_render import host_context, render_role_template

_TDARR = K8S_ROLES / "tdarr"


def _pod() -> dict:
    return yaml_fast.safe_load(render_role_template("tdarr", "deployment.yaml.j2"))[
        "spec"
    ]["template"]["spec"]


def test_the_init_container_encodes_through_vaapi_with_tdarrs_image_and_device() -> (
    None
):
    defaults = load_defaults(_TDARR)
    pod = _pod()
    [init] = pod["initContainers"]
    assert init["image"] == pod["containers"][0]["image"] == defaults["tdarr_k8s_image"]
    script = init["command"][-1]
    assert "-c:v h264_vaapi" in script and "/dev/dri/renderD128" in script, script
    dri = defaults["tdarr_k8s_dri_resource"]
    # Without the device request the render node is absent from the container, and the
    # encode fails on a node whose GPU is fine.
    assert init["resources"]["requests"][dri] == init["resources"]["limits"][dri] == 1
    # tdarr's own transcodes run as PUID with the pod's render group; root would pass a
    # render node tdarr's process cannot open.
    ctx = host_context()
    assert init["securityContext"]["runAsUser"] == ctx["puid"]
    assert ctx["render_gid"] in pod["securityContext"]["supplementalGroups"]


def test_ansible_no_longer_execs_the_encode_into_the_pod() -> None:
    # #5451 closes only once the exec it replaces is gone, so the flip leaves nothing behind
    # that needs `pods/exec`.
    tasks = load_tasks(_TDARR / "tasks" / "verify.yml")
    cmds = [str((t.get("ansible.builtin.command") or {}).get("cmd", "")) for t in tasks]
    assert not [c for c in cmds if "h264_vaapi" in c or "ffmpeg" in c], cmds
