"""`decide` promoting T over a verification in flight (slice D7.5), as clean/flagged pairs.

Each case puts P, `prod`'s commit, in flight over `last-good`'s c0 and publishes T at
master's tip, then runs `promote.decide` with `supersede_on` passed as an argument rather
than patched. A case that supersedes returns at once. A case that refuses falls back to the
in-flight wait, whose fake clock moves `last-good` to P after 90 seconds, so the refusal
reads as a promotion that waited 90 seconds and supersedes nothing.
docs/target-architecture/deploy/d7-superseding-promotions.md has the case table.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_supersede.py
"""

import dataclasses

import pytest
from _promote_fakes import NOW, World, bundle_files, digest_of
from renderer.renderer_lib.snapshot import snapshot_files

import promote


def files(**footprints: str) -> dict[str, bytes]:
    body = {
        n.replace("_", "-"): {"own": v, "footprint": v} for n, v in footprints.items()
    }
    body["_unclassified"] = {"own": "u", "footprint": "u"}
    return bundle_files(body)


class Clock:
    def __init__(self, on_sleep) -> None:
        self.now = 0.0
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.on_sleep(self.now)


@pytest.fixture
def world(tmp_path):
    """c0 is `last-good` and P is `prod` with bento-pdf changed; T at the tip changes sonarr."""
    world = World(tmp_path)
    c0, p, _, tip = world.commits
    world.store(digest_of(c0), files(bento_pdf="a", sonarr="s"))
    world.set_last_good(c0)
    world.set_prod(p, files=files(bento_pdf="b", sonarr="s"))
    world.publish(tip, files=files(bento_pdf="b", sonarr="t"))
    return world


def _decide(world, on: bool = True) -> tuple[promote.Decision, Clock]:
    _, p, *_ = world.commits

    def settle(now: float) -> None:
        if now >= 90:  # P's run settles good and moves last-good to P
            world.set_last_good(p)

    clock = Clock(settle)
    reads = promote.live_reads(world.clients(), world.repo, "master", NOW)
    reads = dataclasses.replace(reads, sleep=clock.sleep, clock=clock)
    return promote.decide(reads, supersede_on=on), clock


def _waited(decision, clock, world) -> bool:
    tip = world.commits[-1]
    return (decision.verdict, decision.target, decision.supersedes, clock.now) == (
        "promote",
        tip,
        "",
        90,
    )


def test_a_disjoint_t_supersedes_p_at_once_and_verifies_against_last_good_is_clean(
    world,
):
    c0, p, _, tip = world.commits
    decision, clock = _decide(world)
    assert (decision.verdict, decision.target, decision.supersedes) == (
        "promote",
        tip,
        p,
    )
    assert clock.now == 0
    assert decision.bundles["prod"] == files(bento_pdf="a", sonarr="s"), (
        "T's changed set is taken against last-good's bundle, so it holds P's bento-pdf"
    )
    assert decision.reset_digest == digest_of(c0)


def test_with_the_flag_off_t_waits_on_p_as_before_is_flagged(world):
    decision, clock = _decide(world, on=False)
    assert _waited(decision, clock, world)


def test_a_p_already_failing_settles_first_is_flagged(world, capsys):
    _, p, *_ = world.commits
    world.transport.statuses[p] = {"flux/bento-pdf": "failure"}
    decision, clock = _decide(world)
    assert _waited(decision, clock, world)
    assert "P already reads failure for bento-pdf" in capsys.readouterr().err


def test_a_t_that_changes_what_p_is_verifying_waits_is_flagged(world, capsys):
    tip = world.commits[-1]
    world.publish(tip, files=files(bento_pdf="c", sonarr="t"))
    decision, clock = _decide(world)
    assert _waited(decision, clock, world)
    assert "T changes bento-pdf, which P is still verifying" in capsys.readouterr().err


def test_a_p_that_itself_superseded_a_run_is_not_superseded_is_flagged(world):
    _, p, *_ = world.commits
    world.transport.tags["superseding"] = digest_of(p)
    decision, clock = _decide(world)
    assert _waited(decision, clock, world)


@pytest.mark.parametrize("name", ["longhorn", "flux", "cluster-vars"])
def test_a_t_in_a_name_with_its_own_window_waits_is_flagged(world, capsys, name):
    _, p, _, tip = world.commits
    key = name.replace("-", "_")
    world.store(
        digest_of(world.commits[0]), files(bento_pdf="a", sonarr="s", **{key: "x"})
    )
    world.set_prod(p, files=files(bento_pdf="b", sonarr="s", **{key: "x"}))
    world.publish(tip, files=files(bento_pdf="b", sonarr="s", **{key: "y"}))
    decision, clock = _decide(world)
    assert _waited(decision, clock, world)
    assert f"T's own change holds {name}" in capsys.readouterr().err


def test_a_widened_p_waits_is_flagged(world, capsys):
    _, p, *_ = world.commits
    widened = files(bento_pdf="b", sonarr="s")
    widened.pop("digests.json")
    world.set_prod(p, files=widened)
    decision, clock = _decide(world)
    assert _waited(decision, clock, world)
    assert "P's set widened to every name" in capsys.readouterr().err


def test_a_hold_is_never_superseded(world):
    world.run.hold = "deadbeef manual"
    decision, clock = _decide(world)
    assert decision.verdict == "held"
    assert clock.now == 0


def test_a_t_that_changes_a_snapshot_role_waits_is_flagged(world, capsys):
    """A failed snapshot-role revision holds without a reset, which supersession does not compose."""
    tip = world.commits[-1]
    snapshot = snapshot_files(
        "sonarr",
        "1111aaaa",
        {"sonarr-config": "media"},
        "img:1",
        3,
        600,
        5,
        540,
        "s",
        "h",
    )
    target = files(bento_pdf="b", sonarr="t")
    target.update(
        {f"cluster/sonarr-snapshot/{n}": t.encode() for n, t in snapshot.items()}
    )
    world.publish(tip, files=target)
    decision, clock = _decide(world)
    assert _waited(decision, clock, world)
    assert decision.stateful == ("sonarr",)
    assert "a forward-only tag or a snapshot role" in capsys.readouterr().err
