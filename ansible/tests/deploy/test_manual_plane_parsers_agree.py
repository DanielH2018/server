"""Three modules parse the deployer's `manual_plane` marker; they must agree (issue #1774).

`DeployerState.manual_plane_pending` writes and reads it, monitor-bridge's
`checks.service._parse_manual_plane` pages off it, and `lib.deployer_park.manual_plane_pending`
puts it on the SessionStart banner. Neither of the last two may import the deployer's tree —
the monitor runs in a pod and `deployer_park` is imported by a hook before anything is on
`sys.path` — so the duplication is deliberate and this test is its price.

The garbled line matters as much as the good ones: all three SKIP a line they cannot parse,
and a reader that started guessing at one would name a role no operator can clear.

Run: uv run pytest ansible/tests/deploy/test_manual_plane_parsers_agree.py
"""

import pathlib
import sys

from _helpers import REPO

# `scripts/` is on pythonpath for its own suites but not for this one, and `deployer_park` is
# reached as `lib.deployer_park` — the same form the SessionStart hook uses.
sys.path.insert(0, str(REPO / "scripts"))

from checks.service import MANUAL_PLANE_CLEAR, _parse_manual_plane
from deploy_remediation import MANUAL_PLANE_CLEAR_CMD
from deploy_state import DeployerState

from lib.deployer_park import MANUAL_PLANE_CLEAR_CMD as PARK_CLEAR_CMD
from lib.deployer_park import manual_plane_pending

# One good line per real pending role, a 3-field line and a line whose stamp is not a number.
_MARKER = "\n".join(
    [
        "abc1230000000000000000000000000000000000 ansible/k3s-bringup.yml k3s 1000",
        "three fields only",
        "beef1230000000000000000000000000000000000 none common 2000",
        "cafe1230000000000000000000000000000000000 none broken not-a-stamp",
    ]
)


def _deployer_pairs(tmp_path) -> list[tuple[str, float]]:
    state = DeployerState(tmp_path)
    pathlib.Path(state.path("manual_plane")).write_text(_MARKER + "\n")
    return [(e.role, e.at) for e in state.manual_plane_pending()]


def test_all_three_parsers_pick_the_same_roles_and_stamps(tmp_path):
    expected = [("k3s", 1000.0), ("common", 2000.0)]
    assert _deployer_pairs(tmp_path) == expected, "the writer's own reader moved"
    assert sorted(_parse_manual_plane(_MARKER)) == sorted(expected)
    assert sorted((r, at) for r, _, at in manual_plane_pending(_MARKER)) == sorted(
        expected
    )


def test_the_banner_and_the_monitor_print_the_deployers_own_clear_command():
    """Non-vacuity: each copy must be a real prefix of the command the deployer documents."""
    assert MANUAL_PLANE_CLEAR in MANUAL_PLANE_CLEAR_CMD
    assert PARK_CLEAR_CMD == MANUAL_PLANE_CLEAR_CMD
    assert "clear-manual-plane" in PARK_CLEAR_CMD
