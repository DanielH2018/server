"""The trim cron's jq picks the same longhorn-manager pod as `ready_manager_ip` (#3736).

`k8s/longhorn-api` and the snapshot reaper call `filter_plugins/longhorn_manager.py`. The trim
cron runs on the host with no repo checkout, so it spells the rule again in jq. This runs the
template's jq program and the function over the same pod lists and asserts they agree, and
that both pick the IP typed out here.
"""

import json
import re
import shutil
import subprocess

import pytest

from _shell_render import rendered_shell_text
from longhorn_manager import ready_manager_ip

NODE = "this-node"


def _pod(ip, *, node=NODE, phase="Running", readies=(True,)):
    status = {"phase": phase, "podIP": ip}
    if readies is not None:
        status["containerStatuses"] = [{"ready": r} for r in readies]
    return {"spec": {"nodeName": node}, "status": status}


CASES = {
    "ready": ([_pod("10.0.0.1")], "10.0.0.1"),
    "a container not ready": ([_pod("10.0.0.1", readies=(True, False))], ""),
    "no containerStatuses": ([_pod("10.0.0.1", readies=None)], ""),
    "empty containerStatuses": ([_pod("10.0.0.1", readies=())], ""),
    "pending": ([_pod("10.0.0.1", phase="Pending")], ""),
    "other node": ([_pod("10.0.0.1", node="other-node")], ""),
    "a ready pod after one with no statuses": (
        [_pod("10.0.0.1", readies=None), _pod("10.0.0.2")],
        "10.0.0.2",
    ),
    "a terminating duplicate before its replacement": (
        [_pod("10.0.0.1", readies=(False,)), _pod("10.0.0.2")],
        "10.0.0.2",
    ),
}


def _trim_jq_program() -> str:
    match = re.search(
        r"""jq -r --arg node "\$NODE" '(.*?)' \| head -1""",
        rendered_shell_text("setup", "k3s", "longhorn-trim-volumes.sh.j2"),
        re.S,
    )
    assert match, "the trim script's manager-pod jq moved; point this test at it"
    return match.group(1)


def _jq_pick(pods: list) -> str:
    result = subprocess.run(
        ["jq", "-r", "--arg", "node", NODE, _trim_jq_program()],
        input=json.dumps({"items": pods}),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    lines = result.stdout.splitlines()
    return lines[0] if lines else ""


@pytest.mark.parametrize("name", CASES)
def test_ready_manager_ip_picks_the_expected_pod(name):
    pods, expected = CASES[name]
    assert ready_manager_ip(pods, NODE) == expected


@pytest.mark.skipif(shutil.which("jq") is None, reason="no jq on this host")
@pytest.mark.parametrize("name", CASES)
def test_the_trim_jq_agrees_with_ready_manager_ip(name):
    pods, expected = CASES[name]
    assert _jq_pick(pods) == ready_manager_ip(pods, NODE) == expected
