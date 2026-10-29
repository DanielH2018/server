"""test_session_health.py must cost the same on daniel-box as on CI's runner (#5432).

Its subprocess test runs session-health.py for real, and the hook's stale-worktree read walks
every `.claude/worktrees/*` checkout on the host until `WORKTREE_TIMEOUT_S` kills it. CI's
runner has no such checkouts, so the file measured 0.77s there (PR #5430's shard 5 of 6, run
39061304325) and 4-5s on daniel-box. `pytest_shard.py`'s weight, recorded on daniel-box, then
trips the shard gate's over-recorded arm on CI.

No test here times the file: a wall-clock bound goes red on a loaded fan-out host with no code
change (#5916). The subprocess test's `_fence_worktree_survey` assertion guards host
independence instead.

Run: uv run pytest .claude/hooks/tests/test_session_health_host_independent.py
"""

import pytest_shard
import shard_weight_gate

_FILE = ".claude/hooks/tests/test_session_health.py"

# What CI's runner measured for _FILE in run 39061304325, quoted in #5432.
_CI_SECONDS = 0.77


def test_recorded_weight_does_not_trip_the_over_recorded_check():
    """The recorded weight sits under the gate's over-recorded ratio of CI's measurement.

    The fix ends with `--record-files` setting the weight from this host. Once the test is
    host-independent, that weight falls below `STALE_WEIGHT_RATIO` x CI's number, so the next
    PR that puts the file in a checked shard no longer trips the arm.
    """
    recorded = pytest_shard.load_weights()[_FILE]
    bound = shard_weight_gate.STALE_WEIGHT_RATIO * _CI_SECONDS
    assert recorded < bound, (
        f"{_FILE} is recorded at {recorded:.2f}s, at or over "
        f"{shard_weight_gate.STALE_WEIGHT_RATIO}x CI's {_CI_SECONDS}s: the shard gate's "
        f"over-recorded arm fires whenever the file lands in a PR's checked shard."
    )
