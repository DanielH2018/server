"""Three modules parse the deployer's `contention_since` marker; they must agree (issue #1847).

`DeployerState.contention_pending` writes and reads it, monitor-bridge's
`checks.gitops._parse_contention` pages off it, and `lib.deployer_park.contention_pending`
puts it on the SessionStart banner. Neither of the last two may import the deployer's tree —
the same arrangement `test_manual_plane_parsers_agree.py` holds together, for the same reasons.

Three facts are pinned: the fields each reader extracts from one good line, that all three
read garbage as no streak, and that the banner's threshold is the number monitor-bridge pages
on — a banner naming a streak the monitor has not paged for, or the reverse, would send an
operator looking for a page that never came.

Run: uv run pytest ansible/tests/deploy/test_contention_parsers_agree.py
"""

import pathlib
import sys

from _helpers import REPO

sys.path.insert(0, str(REPO / "scripts"))

from bridge.config import load_config
from checks.gitops import CONTENTION_CLEAR, _parse_contention
from deploy_remediation import CONTENTION_CLEAR_CMD
from deploy_state import DeployerState

from lib.deployer_park import CONTENTION_CLEAR_CMD as PARK_CLEAR_CMD
from lib.deployer_park import CONTENTION_PARK_SECONDS, contention_pending

_GOOD = "abc1230000000000000000000000000000000000 sonarr 1000.0 1900.0 2"
_GARBLED = ["abc123 sonarr not-a-stamp 1900.0 2", "four fields only here", ""]


def _deployer(tmp_path, text: str) -> tuple[str, float, int] | None:
    state = DeployerState(tmp_path)
    pathlib.Path(state.path("contention")).write_text(text + "\n")
    entry = state.contention_pending()
    return None if entry is None else (entry.lock, entry.first_seen, entry.count)


def test_all_three_parsers_pick_the_same_lock_stamp_and_count(tmp_path):
    expected = ("sonarr", 1000.0, 2)
    assert _deployer(tmp_path, _GOOD) == expected, "the writer's own reader moved"
    assert _parse_contention(_GOOD) == expected
    assert contention_pending(_GOOD) == expected


def test_all_three_parsers_read_garbage_as_no_streak(tmp_path):
    for text in _GARBLED:
        assert _deployer(tmp_path, text) is None, text
        assert _parse_contention(text) == ("", None, 0), text
        assert contention_pending(text) is None, text


def test_the_banner_threshold_is_the_monitors_default():
    assert CONTENTION_PARK_SECONDS == load_config({}).GITOPS_CONTENTION_MAX_S


def test_the_banner_and_the_monitor_print_the_deployers_own_clear_command():
    assert CONTENTION_CLEAR == CONTENTION_CLEAR_CMD
    assert PARK_CLEAR_CMD == CONTENTION_CLEAR_CMD
    assert "clear-contention" in CONTENTION_CLEAR_CMD
