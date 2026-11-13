"""`ready_manager_ip` picks this node's ready longhorn-manager pod (#3736).

`scripts/backup/volume_revert.py` and the snapshot reaper call `template_filters/longhorn_manager.py`. This runs
the function over pod lists and asserts it picks the IP typed out here.
"""

import pytest

from renderer.template_filters.longhorn_manager import ready_manager_ip

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


@pytest.mark.parametrize("name", CASES)
def test_ready_manager_ip_picks_the_expected_pod(name):
    pods, expected = CASES[name]
    assert ready_manager_ip(pods, NODE) == expected
