"""wg-easy's pod drops peer traffic bound for the cluster's pod and service networks.

The k3s wg-easy MASQUERADEs peer traffic out of its own pod, so a peer reaches any ClusterIP
or pod IP as the wg-easy pod unless the pod netns drops it first. The `peer-cluster-drop`
init container inserts one `FORWARD -i wg0 -d <cidr> -j DROP` per cluster CIDR. This pins
three things a drift would silently undo:

- the CIDRs come from `k3s_pod_cidr` / `k3s_service_cidr`, not literals — the clean case
  renders at CIDRs the inventory does not hold;
- the rules are INSERTED at position 1, because wg-easy's PostUp appends its
  `-A FORWARD -i wg0 -j ACCEPT` after this runs, and an appended DROP would sit below it;
- they run in an init container, which is what orders them before that PostUp.

Why an init container rather than a sidecar is the `DECIDED:` marker in the template.

Run: uv run pytest ansible/tests/services/test_wg_easy_peer_cluster_drop.py
"""

import copy
import re

import pytest

from _k8s_render import render_role_template
from lib import yaml_fast

_INIT = "peer-cluster-drop"
_POD = "10.99.0.0/16"
_SVC = "10.98.0.0/16"


def _deployment(overrides: dict | None = None) -> dict:
    docs = yaml_fast.safe_load_all(
        render_role_template("wg-easy", "deployment.yaml.j2", overrides)
    )
    return next(
        d for d in docs if isinstance(d, dict) and d.get("kind") == "Deployment"
    )


def _problems(deployment: dict, cidrs: list[str]) -> list[str]:
    spec = deployment["spec"]["template"]["spec"]
    init = next((c for c in spec.get("initContainers", []) if c["name"] == _INIT), None)
    if init is None:
        return [f"no init container named {_INIT!r}"]
    script = "\n".join(init["command"])
    problems = []
    for cidr in cidrs:
        insert = rf"iptables -I FORWARD 1 -i wg0 -d {re.escape(cidr)} -j DROP"
        if not re.search(insert, script):
            problems.append(f"no insert-at-1 DROP for {cidr}")
    if re.search(r"-A FORWARD", script):
        problems.append("appends to FORWARD, which lands below wg-easy's PostUp ACCEPT")
    return problems


def test_drop_rules_follow_the_cluster_cidr_vars_is_clean():
    deployment = _deployment({"k3s_pod_cidr": _POD, "k3s_service_cidr": _SVC})
    assert _problems(deployment, [_POD, _SVC]) == []


def _without_init(doc):
    spec = doc["spec"]["template"]["spec"]
    spec["initContainers"] = [c for c in spec["initContainers"] if c["name"] != _INIT]


def _appending(doc):
    for c in doc["spec"]["template"]["spec"]["initContainers"]:
        if c["name"] == _INIT:
            c["command"] = [
                s.replace("-I FORWARD 1", "-A FORWARD") for s in c["command"]
            ]


def _dropping_one_cidr(doc):
    for c in doc["spec"]["template"]["spec"]["initContainers"]:
        if c["name"] == _INIT:
            c["command"] = [
                "\n".join(line for line in s.splitlines() if _SVC not in line)
                for s in c["command"]
            ]


@pytest.mark.parametrize("mutate", [_without_init, _appending, _dropping_one_cidr])
def test_drop_rules_is_flagged(mutate):
    deployment = copy.deepcopy(
        _deployment({"k3s_pod_cidr": _POD, "k3s_service_cidr": _SVC})
    )
    mutate(deployment)
    assert _problems(deployment, [_POD, _SVC]) != []
