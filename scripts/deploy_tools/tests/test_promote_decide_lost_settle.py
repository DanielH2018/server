"""How `decide`'s wait on a verification in flight ends when nothing settles it (#7863).

A run that is still verifying keeps the wait going to IN_FLIGHT_WAIT_S. A run that ended
without its `settle` is named once no other run past its `decide` has been in progress for
LOST_SETTLE_S. The tests run `promote.decide` over the fake transport in `_promote_fakes.py`,
with a fake clock in `Reads`, as `test_promote_decide_verification.py` does.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_decide_lost_settle.py
"""

import dataclasses

import pytest
from _promote_fakes import NOW, World, digest_of

import promote
from deploy_tools.promote_lib import verify_promotion as vp


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


class Clock:
    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


def _reads(world, clock: Clock) -> promote.Reads:
    reads = promote.live_reads(world.clients(), world.repo, "master", NOW)
    return dataclasses.replace(reads, sleep=clock.sleep, clock=clock)


def test_a_verification_that_never_settles_stalls_as_current(world):
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.publish(tip)
    _verifying(world, 7, c1)
    clock = Clock()
    decision = promote.decide(_reads(world, clock), supersede_on=False)
    assert decision.verdict == "current"
    assert "never settled" in decision.reason
    assert (decision.lost_settle, decision.lost_settle_digest) == ("", "")
    assert clock.now >= vp.IN_FLIGHT_WAIT_S


def _verifying(world, run_id: int, head_sha: str) -> None:
    """List an in-progress run whose `promote` job has finished, so it is verifying."""
    world.transport.runs.append({"id": run_id, "head_sha": head_sha})
    world.transport.jobs[run_id] = [
        {"name": "promote", "status": "completed"},
        {"name": "verify", "status": "in_progress"},
    ]


def _lost_settle(world):
    """The #7863 tags: c1 superseded a run, so `prod` = `superseding` = c1 != `last-good`."""
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.store(digest_of(c0))
    world.transport.tags["superseding"] = digest_of(c1)
    world.publish(tip)
    return c0, c1


def test_a_run_that_ended_without_its_settle_is_named_with_the_operator_command(
    world, monkeypatch
):
    c0, c1 = _lost_settle(world)
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    world.transport.runs = [{"id": 9, "head_sha": world.commits[-1]}]  # this run only
    clock = Clock()
    decision = promote.decide(_reads(world, clock), supersede_on=True)
    assert decision.verdict == "current"
    assert f"the run that moved prod to {c1} ended without a settle" in decision.reason
    assert (
        f"flux tag artifact oci://ghcr.io/danielh2018/homelab-manifests@{digest_of(c1)} "
        "--tag last-good" in decision.reason
    )
    assert f"--body '{c1} unverified'" in decision.reason
    assert (decision.lost_settle, decision.lost_settle_digest) == (c1, digest_of(c1))
    assert clock.now == vp.LOST_SETTLE_S
    assert world.transport.tags["last-good"] == digest_of(c0)


def test_a_lost_settle_whose_last_good_bundle_is_unreadable_is_an_error(
    world, monkeypatch
):
    """Without the hold form, `reconcile` must not run and write the plain `unverified`."""
    c0, c1 = _lost_settle(world)
    del world.transport.manifests[digest_of(c0)]
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    world.transport.runs = [{"id": 9, "head_sha": world.commits[-1]}]
    decision = promote.decide(_reads(world, Clock()), supersede_on=True)
    assert decision.verdict == "error"
    assert f"Reading what {c1} changes against last-good" in decision.reason
    assert "last-good's bundle" in decision.reason
    assert (decision.lost_settle, decision.lost_settle_digest) == ("", "")


def test_a_run_still_in_progress_restarts_the_lost_settle_count(world):
    _, c1 = _lost_settle(world)
    _verifying(world, 7, c1)

    def finish(now: float) -> None:
        if now >= 300:  # c1's run ends, its settle never having run
            world.transport.runs = []

    clock = Clock(finish)
    decision = promote.decide(_reads(world, clock), supersede_on=True)
    assert "ended without a settle" in decision.reason
    assert clock.now == 300 + vp.LOST_SETTLE_S


def test_a_later_run_queued_behind_this_decide_does_not_hold_off_the_lost_settle(world):
    _, c1 = _lost_settle(world)
    tip = world.commits[-1]
    # c1's own run ended without its settle; a later merge's run waits on the promote group.
    world.transport.runs = [
        {"id": 6, "head_sha": c1, "status": "completed"},
        {"id": 8, "head_sha": tip, "status": "in_progress"},
    ]
    world.transport.jobs[6] = [{"name": "promote", "status": "completed"}]
    world.transport.jobs[8] = [
        {"name": "publish", "status": "completed"},
        {"name": "promote", "status": "queued"},
    ]
    clock = Clock()
    decision = promote.decide(_reads(world, clock), supersede_on=True)
    assert "ended without a settle" in decision.reason
    assert clock.now == vp.LOST_SETTLE_S


@pytest.mark.parametrize("status", [502, 403])
def test_a_failed_listing_of_the_promote_runs_keeps_the_wait_going(world, status):
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.publish(tip)
    world.transport.api_forced[
        "/repos/DanielH2018/server/actions/workflows/promote.yml/runs"
    ] = status

    def settle(now: float) -> None:
        if now >= vp.LOST_SETTLE_S + 90:  # c1's run settles good after the window
            world.set_last_good(c1)

    clock = Clock(settle)
    decision = promote.decide(_reads(world, clock), supersede_on=False)
    assert (decision.verdict, decision.target) == ("promote", tip)
