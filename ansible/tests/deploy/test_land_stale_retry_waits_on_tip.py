"""land.sh's exit-4 retry waits for master CI on the NEW tip before re-ticking.

Step 3 waits on the PR's own merge commit. When another PR merges during that wait, the
tick refuses to fast-forward until CI is green on the tip (`origin <tip>: CI not finished
— deferring`), so a bare re-tick defers again and deploy.sh exits 4 a second time. Three
landings on 2026-09-02 ended `VERDICT: deploy-failed (exit 4)` that way with nothing
deployed, each settled by hand with `await_ci.py <tip>` and a second land.sh.

Textual, like test_landing_annotations.py: land.sh has no cheap live test, and the shape
being guarded is the ORDER of three calls inside one bounded `while` loop.
"""

from __future__ import annotations

import re

from _helpers import REPO as _REPO

_LAND_SH = _REPO / "scripts/deploy_tools/land.sh"


_LOOP_HEAD = (
    'while [ "$deploy_rc" -eq 4 ] && [ "$stale_attempt" -lt "$STALE_RETRIES" ]; do'
)


def _stale_retry_block(text: str) -> str:
    start = text.index(_LOOP_HEAD)
    end = text.index("\ndone\n", start)
    return text[start:end]


def _order_is_blockers_then_tip_wait_then_tick(block: str) -> bool:
    blockers = block.find("deploy_tags.py blockers")
    wait = block.find("await_ci.py")
    tick = block.find("gitops_tick.sh")
    if min(blockers, wait, tick) < 0:
        return False
    if not (blockers < wait < tick):
        return False
    # The wait must target the fetched tip, not the PR's own merge commit step 3 already
    # settled -- waiting on MERGE_SHA again would return instantly and change nothing.
    return bool(re.search(r'await_ci\.py "\$TIP_SHA"', block))


def test_stale_retry_waits_on_the_tip_after_blockers_and_before_the_tick():
    assert _order_is_blockers_then_tip_wait_then_tick(
        _stale_retry_block(_LAND_SH.read_text())
    )


def test_a_retry_that_reticks_without_waiting_is_flagged():
    """The pre-2026-09-02 shape: blockers, then straight to the tick."""
    block = _stale_retry_block(_LAND_SH.read_text())
    stripped = re.sub(r"  TIP_SHA=.*?\n  fi\n", "", block, count=1, flags=re.DOTALL)
    assert "await_ci.py" not in stripped, "the mutation did not remove the wait"
    assert not _order_is_blockers_then_tip_wait_then_tick(stripped)


def test_a_retry_that_waits_on_the_merge_commit_instead_of_the_tip_is_flagged():
    block = _stale_retry_block(_LAND_SH.read_text())
    mutated = block.replace('await_ci.py "$TIP_SHA"', 'await_ci.py "$MERGE_SHA"')
    assert not _order_is_blockers_then_tip_wait_then_tick(mutated)


def test_the_tip_wait_maps_red_and_no_verdict_like_step_three():
    """Same exit-code contract as step 3: 1 = red, 75 = no verdict inside the budget."""
    block = _stale_retry_block(_LAND_SH.read_text())
    assert re.search(r"^\s*1\) die .*RED", block, re.MULTILINE)
    assert re.search(r"^\s*75\) die .* 75 ci-timeout ;;", block, re.MULTILINE)


def _retry_bound(text: str) -> int:
    m = re.search(r"^STALE_RETRIES=(\d+)$", text, re.MULTILINE)
    assert m, "STALE_RETRIES is not a literal assignment"
    return int(m.group(1))


def test_the_retry_is_bounded_above_one_and_below_forever():
    """One retry lost to a third merge during the tip wait (2026-09-02); unbounded would
    hide a stalled landing. Each pass re-runs the blockers check, so more is safe."""
    text = _LAND_SH.read_text()
    assert 2 <= _retry_bound(text) <= 5
    block = _stale_retry_block(text)
    assert block.startswith(_LOOP_HEAD)
    assert "stale_attempt=$((stale_attempt + 1))" in block
    assert block.count("deploy_tags.py blockers") == 1, (
        "the blockers check runs every pass"
    )


def test_a_loop_without_a_counter_step_is_flagged():
    block = _stale_retry_block(_LAND_SH.read_text())
    mutated = block.replace("stale_attempt=$((stale_attempt + 1))", "")
    assert "stale_attempt=$((stale_attempt + 1))" not in mutated


def _books_tip_wait_under_wait_ci(block: str) -> bool:
    """Both stamps must shift by the same waited seconds: T_CI alone would also stretch
    `tick`, T_TICK alone would shrink `deploy` without crediting `wait_ci`."""
    return (
        "T_CI=$((T_CI + tip_waited))" in block
        and "T_TICK=$((T_TICK + tip_waited))" in block
        and block.find("await_ci.py") < block.find("T_CI=$((T_CI + tip_waited))")
    )


def test_the_tip_wait_is_booked_under_wait_ci_not_deploy():
    assert _books_tip_wait_under_wait_ci(_stale_retry_block(_LAND_SH.read_text()))


def test_shifting_only_one_stamp_is_flagged():
    block = _stale_retry_block(_LAND_SH.read_text())
    assert not _books_tip_wait_under_wait_ci(
        block.replace("T_TICK=$((T_TICK + tip_waited))", "")
    )
    assert not _books_tip_wait_under_wait_ci(
        block.replace("T_CI=$((T_CI + tip_waited))", "")
    )
