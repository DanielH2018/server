"""qbittorrent's reach probe names its pod per run, so another pod cannot block the verify.

`kubectl run <name>` refuses a name that already exists, and the deploy then fails after a good
rollout. Two cases leave a pod by that name behind. One is an overlapping qbittorrent verify:
the 2026-10-12 landing of #4552 failed this way. The other is a pod orphaned when the Ansible
controller died mid-probe. `--rm` cannot clean that one up, because the kubectl client that
deletes the pod died with the controller (#4556).

The NetworkPolicy admits the probe by its `app` label, not by its pod name. So the label must
stay fixed while the name varies.

Run: uv run pytest ansible/tests/services/test_qbittorrent_reach_probe_name.py
"""

import re

from ansible.template import Templar, trust_as_template
from _helpers import K8S_ROLES, load_tasks, task_named

VERIFY = K8S_ROLES / "qbittorrent" / "tasks" / "verify.yml"
PROBE_LABEL = "qbittorrent-reach-probe"
# A DNS-1123 label. Kubernetes derives the pod's hostname from its name, so this is the
# stricter of the two shapes a pod name can take.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _rendered_cmd() -> str:
    """The probe task's whole command, templated through Ansible as one deploy would.

    The whole string is templated rather than an extracted name expression, so the test reads
    the name the task actually passes to `kubectl run`.
    """
    task = task_named(load_tasks(VERIFY), "Reach the WebUI from a pod")
    templar = Templar(
        loader=None,
        variables={
            "k8s_namespace": "homelab",
            "qbittorrent_k8s_probe_image": "alpine",
            "container_item": {"port": 8080},
        },
    )
    return str(
        templar.template(trust_as_template(task["ansible.builtin.command"]["cmd"]))
    )


def _pod_name(cmd: str) -> str:
    match = re.search(r"\brun (\S+)", cmd)
    assert match, f"no `kubectl run <name>` in {cmd!r}"
    return match.group(1)


def test_each_run_names_its_probe_pod_afresh():
    first, second = _pod_name(_rendered_cmd()), _pod_name(_rendered_cmd())
    assert first != second, (
        f"two runs both named the probe pod {first!r}, so the second run fails AlreadyExists "
        "against the first run's pod, or against a pod a dead controller left behind"
    )


def test_the_probe_pod_name_is_one_kubernetes_accepts():
    name = _pod_name(_rendered_cmd())
    assert name.startswith(f"{PROBE_LABEL}-"), name
    assert DNS_LABEL.match(name) and len(name) <= 63, (
        f"{name!r} is not a DNS-1123 label of at most 63 bytes, so kubectl refuses the pod"
    )


def test_the_probe_label_stays_the_one_the_fence_admits():
    # Rendered, not read as text. test_netpol_from.py checks for the substring
    # `--labels app=qbittorrent-reach-probe`, which a label carrying the per-run suffix still
    # contains, so that guard alone would pass a label the NetworkPolicy no longer matches.
    match = re.search(r"--labels app=(\S+)", _rendered_cmd())
    assert match and match.group(1) == PROBE_LABEL, (
        f"the probe's app label must be exactly {PROBE_LABEL!r}, the caller the qbittorrent "
        f"NetworkPolicy admits; got {match and match.group(1)!r}"
    )
