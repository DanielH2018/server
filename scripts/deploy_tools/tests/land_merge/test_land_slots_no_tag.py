"""A landing whose promotion gates no tag gives its slot up, so a serial PR's drain ends.

Each case runs one landing's real promotion wait (`promotion.wait_on_promotion` over
`promote_wait.land_wait` and a `Sim`) inside a real slot, and reads a serial PR's drain bar
(`slots._bar`) at every poll of that wait. The slots are lock files in the test's own turn
directory (`turn.TURN_DIR_ENV`).

Run: uv run pytest scripts/deploy_tools/tests/land_merge/test_land_slots_no_tag.py
"""

from dataclasses import replace

import pytest

from _land_fakes import Fakes, make_landing
from _land_wait_fakes import M, P, Sim, at, digests
from deploy_tools.land_lib import live_limit, promotion, slots
from deploy_tools.land_lib.outcome import Outcome, Verdict
from deploy_tools.promote_lib import wait as promote_wait

_UNCHANGED = {M: digests(sonarr="s"), P: digests(sonarr="s")}
_SONARR_MOVED = {M: digests(sonarr="t"), P: digests(sonarr="s")}


def _on_the_lock_files():
    ln, _ = make_landing(Fakes())
    ln.tools = replace(ln.tools, try_slot=None)
    return ln


def _wait_beside_a_serial_pr(sim: Sim, bars: list) -> None:
    """Run a landing's promotion wait in a slot, adding the serial PR's drain bar at each poll."""
    ln, serial = _on_the_lock_files(), _on_the_lock_files()
    ln.merge_sha = M

    def sleep(_seconds):
        bars.append(slots._bar(serial, live_limit.SERIAL, slots._lock_of(serial)))
        sim.sleeps += 1

    def land_wait(sha, services, primary, cap_s, window_grace_s, on_root_alone):
        return promote_wait.land_wait(
            replace(sim.reads(), sleep=sleep),
            sha,
            services,
            cap_s=cap_s,
            window_grace_s=window_grace_s,
            on_root_alone=on_root_alone,
        )

    ln.tools = replace(ln.tools, flux=replace(ln.tools.flux, land_wait=land_wait))
    with slots.held(ln):
        promotion.wait_on_promotion(ln)


def test_a_serial_drain_ends_while_a_homelab_only_landing_still_waits():
    bars = []
    _wait_beside_a_serial_pr(Sim(polls=[at(P), at(M)], published=_UNCHANGED), bars)
    assert bars == [None]


@pytest.mark.parametrize(
    "sim",
    [
        Sim(polls=[at(P, "sonarr"), at(M, "sonarr")], published=_SONARR_MOVED),
        Sim(polls=[at(P), at(M)], published={P: digests(sonarr="s")}),
    ],
    ids=["a-tag-in-the-changed-set", "the-changed-set-unread"],
)
def test_a_serial_drain_waits_for_a_landing_that_may_gate_a_tag(sim):
    bars = []
    _wait_beside_a_serial_pr(sim, bars)
    (bar,) = bars
    assert bar and bar.clears is slots.Clears.VERDICTS


def test_a_homelab_only_landing_without_its_slot_still_ends_deploy_failed_on_a_hold():
    sim = Sim(
        polls=[at(P)], holds=["none", "held by the operator"], published=_UNCHANGED
    )
    bars = []
    with pytest.raises(Outcome) as ended:
        _wait_beside_a_serial_pr(sim, bars)
    assert ended.value.verdict == Verdict.DEPLOY_FAILED
    assert bars == [None]
