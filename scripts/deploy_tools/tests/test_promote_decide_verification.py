"""`promote.py decide`'s half of verification: the pulls, the seed, the wait and the revert rule.

The tests run `decide` against the real forge clients over the fake transport in
`_promote_fakes.py`, as `test_promote_decide.py` does. The in-flight wait and the revert rule
call `promote.decide` directly, with a fake clock in `Reads` and the rule passed as an
argument rather than patched.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_decide_verification.py
"""

import argparse
import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from _promote_fakes import NOW, World, bundle_files, digest_of
from renderer.renderer_lib.bundle import tag_kustomization

import promote
from deploy_tools.promote_lib import verify_handoff
from deploy_tools.promote_lib import verify_promotion as vp


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _digests(**footprints: str) -> dict[str, bytes]:
    body = {
        n.replace("_", "-"): {"own": v, "footprint": v} for n, v in footprints.items()
    }
    body["_unclassified"] = {"own": "u", "footprint": "u"}
    return bundle_files(body)


def _decide(world, monkeypatch, capsys, trigger: str = ""):
    """`decide` through the CLI with `--bundle-dir`, as the promote job runs it."""
    bundle_dir = world.tmp_path / "verify-input"
    output = world.tmp_path / "github_output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["decide", "--trigger", trigger, "--repo", str(world.repo)]
    argv += ["--master-ref", "master", "--bundle-dir", str(bundle_dir)]
    exit_code = promote.main(argv, f=world.clients(), now=NOW)
    err = capsys.readouterr().err
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return exit_code, outputs, bundle_dir, err


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


def _reads(world, clock: Clock | None = None) -> promote.Reads:
    clock = clock or Clock()
    reads = promote.live_reads(world.clients(), world.repo, "master", NOW)
    return dataclasses.replace(reads, sleep=clock.sleep, clock=clock)


# -- the pulls and the seed ------------------------------------------------------------------


def test_a_promotion_hands_both_bundles_to_verify(world, monkeypatch, capsys):
    c0, *_, tip = world.commits
    world.set_prod(c0, files=_digests(bento_pdf="a"))
    world.set_last_good(c0)
    world.publish(tip, files=_digests(bento_pdf="b"))
    exit_code, outputs, bundle_dir, _ = _decide(world, monkeypatch, capsys, tip)
    assert (exit_code, outputs["verdict"], outputs["prior"]) == (0, "promote", c0)
    prod, target = verify_handoff.read_handoff(bundle_dir)
    assert prod is not None
    assert json.loads(prod["digests.json"])["bento-pdf"]["footprint"] == "a"
    assert json.loads(target["digests.json"])["bento-pdf"]["footprint"] == "b"


def test_a_failed_pull_of_sha_t_is_an_error_and_moves_nothing(
    world, monkeypatch, capsys
):
    c0, *_, tip = world.commits
    world.set_prod(c0)
    world.publish(tip)
    layer = world.transport.manifests[digest_of(tip)]["layers"][0]["digest"]
    del world.transport.blobs[layer]
    exit_code, outputs, bundle_dir, err = _decide(world, monkeypatch, capsys, tip)
    assert (exit_code, outputs["verdict"], outputs["target"]) == (1, "error", "")
    assert f"blob {layer}: HTTP 404" in err
    assert not bundle_dir.exists()


def test_an_absent_last_good_is_seeded_from_prods_digest(world, monkeypatch, capsys):
    c0, *_, tip = world.commits
    world.set_prod(c0)
    world.publish(tip)
    _, outputs, _, _ = _decide(world, monkeypatch, capsys, tip)
    assert (outputs["seed"], outputs["reset_digest"]) == ("true", digest_of(c0))
    assert outputs["prior_digest"] == digest_of(c0), "tag compares prod against this"


def test_a_present_last_good_is_the_reset_target_and_is_not_seeded(
    world, monkeypatch, capsys
):
    c0, *_, tip = world.commits
    world.set_prod(c0)
    world.set_last_good(c0)
    world.publish(tip)
    _, outputs, _, _ = _decide(world, monkeypatch, capsys, tip)
    assert (outputs["seed"], outputs["reset_digest"]) == ("false", digest_of(c0))


def test_a_first_run_seeds_nothing_and_hands_on_no_prod(world, monkeypatch, capsys):
    tip = world.commits[-1]
    world.publish(tip)
    _, outputs, bundle_dir, _ = _decide(world, monkeypatch, capsys, tip)
    assert (outputs["verdict"], outputs["seed"], outputs["reset_digest"]) == (
        "promote",
        "false",
        "",
    )
    assert verify_handoff.read_handoff(bundle_dir)[0] is None


def test_verify_waits_on_t_not_on_the_triggering_commit(world, monkeypatch, capsys):
    """`decide` picks T newer than X; statuses posted on X alone time out into `failed`."""
    c0, _, x, t = world.commits
    world.set_prod(c0, files=_digests(bento_pdf="a"))
    world.set_last_good(c0)
    world.publish(x, files=_digests(bento_pdf="b"))
    world.publish(t, files=_digests(bento_pdf="b"))
    _, outputs, bundle_dir, _ = _decide(world, monkeypatch, capsys, x)
    assert outputs["target"] == t
    prod, target = verify_handoff.read_handoff(bundle_dir)
    expected = vp.expected_set(target, prod)
    assert expected.names == {"homelab", "bento-pdf"}
    statuses = world.clients().github.statuses
    clock = Clock()
    reads = vp.StatusReads(statuses, clock.sleep, clock)
    world.transport.statuses[x] = {f"flux/{n}": "success" for n in expected.names}
    assert vp.verify(reads, t, c0, expected).verdict == "failed"
    world.transport.statuses[t] = {f"flux/{n}": "success" for n in expected.names}
    assert vp.verify(reads, t, c0, expected).verdict == "good"


# -- the wait on a verification in flight ----------------------------------------------------


def test_decide_waits_until_prod_and_last_good_agree_then_promotes(world):
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.publish(tip)

    def settle(now: float) -> None:
        if now >= 90:  # the in-flight run's `settle` moves last-good to prod
            world.set_last_good(c1)

    clock = Clock(settle)
    decision = promote.decide(_reads(world, clock), supersede_on=False)
    assert (decision.verdict, decision.target) == ("promote", tip)
    assert clock.now == 90


def test_a_verification_that_never_settles_stalls_as_current(world):
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.publish(tip)
    clock = Clock()
    decision = promote.decide(_reads(world, clock), supersede_on=False)
    assert decision.verdict == "current"
    assert "never settled" in decision.reason
    assert clock.now >= vp.IN_FLIGHT_WAIT_S


def test_a_hold_written_during_the_wait_ends_it_as_held(world):
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.publish(tip)

    def fail(now: float) -> None:
        world.run.hold = f"{c1} failed=bento-pdf"

    decision = promote.decide(_reads(world, Clock(fail)), supersede_on=False)
    assert decision.verdict == "held"
    assert decision.reason == f"DEPLOY_HOLD={c1} failed=bento-pdf"


_502 = "failed to get variable DEPLOY_HOLD: HTTP 502: Server Error"
_401 = "HTTP 401: Bad credentials"


def _in_flight(world) -> tuple[str, str]:
    """`prod` on c1 while `last-good` is still c0, so `decide` waits; returns (c1, tip)."""
    c0, c1, _, tip = world.commits
    world.set_prod(c1)
    world.set_last_good(c0)
    world.publish(tip)
    return c1, tip


@pytest.mark.parametrize(
    ("errors", "verdict", "reason", "unread"),
    [
        ([_502] * 2, "promote", "", 0),
        ([_502] * 4, "error", "HTTP 502", 1),
        ([_401] * 3, "error", "HTTP 401", 2),
    ],
    ids=["5xx-recovers", "5xx-gives-up-after-three-reads", "4xx-read-once"],
)
def test_a_5xx_reading_the_hold_mid_wait_is_retried_and_nothing_else_is(
    world, errors, verdict, reason, unread
):
    c1, _ = _in_flight(world)

    def settle(now: float) -> None:
        if now == vp.IN_FLIGHT_POLL_S:  # one poll in, GitHub errors on the next reads
            world.run.hold_errors = list(errors)
        if now >= 90:
            world.set_last_good(c1)

    decision = promote.decide(_reads(world, Clock(settle)), supersede_on=False)
    assert decision.verdict == verdict, decision.reason
    assert reason in decision.reason
    # `unread` pins how many reads ran: three for a 5xx, one for anything else.
    assert len(world.run.hold_errors) == unread


def test_a_5xx_reading_a_digest_mid_wait_is_retried(world):
    c1, tip = _in_flight(world)

    def settle(now: float) -> None:
        if now == vp.IN_FLIGHT_POLL_S:  # the next read of `prod` answers 502 once
            world.transport.forced["prod"] = 502
        else:
            world.transport.forced.pop("prod", None)
        if now >= 90:
            world.set_last_good(c1)

    decision = promote.decide(_reads(world, Clock(settle)), supersede_on=False)
    assert (decision.verdict, decision.target) == ("promote", tip), decision.reason


def test_a_5xx_reading_the_superseding_digest_mid_wait_is_retried(world):
    c1, tip = _in_flight(world)
    # `prod`'s run itself superseded one, so T may not take it over and the wait goes on.
    world.transport.tags["superseding"] = digest_of(c1)

    def settle(now: float) -> None:
        if (
            now == vp.IN_FLIGHT_POLL_S
        ):  # the next read of `superseding` answers 502 once
            world.transport.forced["superseding"] = 502
        else:
            world.transport.forced.pop("superseding", None)
        if now >= 90:
            world.set_last_good(c1)

    decision = promote.decide(_reads(world, Clock(settle)), supersede_on=True)
    assert (decision.verdict, decision.target) == ("promote", tip), decision.reason


# -- the revert rule -------------------------------------------------------------------------


def _held_world(world, revert_files: dict[str, bytes]) -> tuple[str, str]:
    """T failed on bento-pdf and `prod` was reset to `last-good`; U is the revert after T."""
    good, _, t, u = world.commits
    world.set_prod(good, files=_digests(bento_pdf="a"))
    world.set_last_good(good)
    world.publish(t, files=_digests(bento_pdf="b"))
    world.publish(u, files=revert_files)
    world.run.hold = f"{t} failed=bento-pdf"
    return t, u


def test_with_the_flag_off_a_failed_hold_holds_a_matching_revert(world):
    _held_world(world, _digests(bento_pdf="a"))
    assert promote.REVERT_RULE is False
    decision = promote.decide(_reads(world))
    assert decision.verdict == "held"
    assert world.transport.registry_requests() == [], "a held run reads nothing else"


def test_with_the_flag_on_a_revert_matching_last_good_promotes_and_clears(world):
    _, u = _held_world(world, _digests(bento_pdf="a"))
    decision = promote.decide(_reads(world), revert_rule=True)
    assert (decision.verdict, decision.target, decision.clears_hold) == (
        "promote",
        u,
        True,
    )


def test_with_the_flag_on_a_candidate_whose_failed_footprint_differs_stays_held(world):
    _held_world(world, _digests(bento_pdf="c"))
    decision = promote.decide(_reads(world), revert_rule=True)
    assert decision.verdict == "held"
    assert "no descendant of it matches" in decision.reason


@pytest.mark.parametrize(
    "hold",
    ["{t} broke the drill", "{t} failed=soak", "{t} unverified"],
    ids=["free-text", "soak", "unverified"],
)
def test_with_the_flag_on_a_hold_in_another_form_stays_held(world, hold):
    t, _ = _held_world(world, _digests(bento_pdf="a"))
    world.run.hold = hold.format(t=t)
    assert promote.decide(_reads(world), revert_rule=True).verdict == "held"


def test_a_revert_bundle_with_no_digests_json_never_qualifies(world):
    _held_world(world, bundle_files(None))
    assert promote.decide(_reads(world), revert_rule=True).verdict == "held"


def test_the_failed_hold_form_is_the_one_settle_writes():
    """`settle` writes `<T> failed=<names>`; the revert rule's pattern must read that form."""
    t = hashlib.sha1(b"t").hexdigest()
    assert vp.revertible(f"{t} failed=bento-pdf,sonarr", True) == (
        t,
        frozenset({"bento-pdf", "sonarr"}),
    )
    assert vp.revertible(f"{t} failed=bento-pdf", False) is None


def test_the_status_read_maps_each_context_to_its_newest_state(world):
    t = world.commits[-1]
    world.transport.statuses[t] = {
        "flux/homelab": "success",
        "flux/bento-pdf": "pending",
    }
    assert world.clients().github.statuses(t) == world.transport.statuses[t]


def test_a_status_read_that_answers_500_is_a_forge_error(world):
    import forge

    t = world.commits[-1]
    world.transport.api_forced[f"/repos/DanielH2018/server/commits/{t}/status"] = 500
    with pytest.raises(forge.ForgeError, match="HTTP 500"):
        world.clients().github.statuses(t)


# -- statuses an earlier promotion left on T -------------------------------------------------

MOVED_AT = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)


def _repromoted(world) -> tuple[str, vp.Expected, vp.StatusReads]:
    """T held on `failure`, then promoted again: its statuses predate the second move."""
    t = world.commits[-1]
    world.transport.statuses[t] = {
        "flux/homelab": "success",
        "flux/bento-pdf": "failure",
    }
    world.transport.status_times[t] = "2026-10-15T00:30:00Z"
    expected = vp.Expected(frozenset({"homelab", "bento-pdf"}), frozenset(), "", 600)
    clock = Clock()
    return (
        t,
        expected,
        vp.StatusReads(world.clients().github.statuses, clock.sleep, clock),
    )


def test_a_failure_an_earlier_promotion_left_on_t_is_not_read_as_this_ones_is_clean(
    world,
):
    t, expected, reads = _repromoted(world)
    since = MOVED_AT - timedelta(seconds=vp.MOVED_AT_SKEW_S)
    assert vp.verify(reads, t, "", expected, since) == vp.Verdict(
        "failed", ("bento-pdf", "homelab")
    ), "nothing fresh arrived, so the timeout names both, not the stale failure"
    world.transport.status_times[t] = "2026-10-15T01:02:00Z"
    world.transport.statuses[t]["flux/bento-pdf"] = "success"
    assert vp.verify(reads, t, "", expected, since) == vp.Verdict("good")


def test_a_failure_an_earlier_promotion_left_on_t_holds_at_once_is_flagged(world):
    """The red half: an unbounded read takes the old failure as this promotion's."""
    t, expected, reads = _repromoted(world)
    clock_before = reads.clock()
    assert vp.verify(reads, t, "", expected) == vp.Verdict("failed", ("bento-pdf",))
    assert reads.clock() == clock_before, "it held on the first read, without waiting"


def test_a_status_with_no_update_time_reads_as_none_once_bounded(world):
    t = world.commits[-1]
    world.transport.statuses[t] = {"flux/homelab": "success"}
    assert world.clients().github.statuses(t, MOVED_AT) == {}
    assert world.clients().github.statuses(t) == {"flux/homelab": "success"}


# -- the record of prod's statuses (H1.1i) ---------------------------------------------------


def _outage(cluster_base: str) -> dict[str, bytes]:
    """`longhorn` depending on `cluster-base`, whose footprint is `cluster_base`."""
    longhorn = tag_kustomization("longhorn", substitutes=False, timeout="10m")
    longhorn["spec"]["dependsOn"] = [{"name": "cluster-base"}]
    files = _digests(cluster_base=cluster_base, longhorn="l")
    files["flux/cluster-base.yaml"] = yaml.safe_dump(
        tag_kustomization("cluster-base", substitutes=False, timeout="10m")
    ).encode()
    files["flux/longhorn.yaml"] = yaml.safe_dump(longhorn).encode()
    return files


def _status_path(sha: str) -> str:
    return f"/repos/DanielH2018/server/commits/{sha}/status"


def test_verify_excuses_from_decides_record_and_never_reads_prods_commit(
    world, monkeypatch, capsys
):
    """The record is what `prod`'s commit read before the move; a later read cannot change it."""
    c0, *_, tip = world.commits
    world.set_prod(c0, files=_outage("a"))
    world.set_last_good(c0)
    world.publish(tip, files=_outage("b"))
    world.transport.statuses[c0] = {"flux/longhorn": "failure"}
    _, outputs, bundle_dir, err = _decide(world, monkeypatch, capsys, tip)
    assert outputs["verdict"] == "promote"
    assert f"recorded on prod's commit {c0}: flux/longhorn=failure" in err
    assert verify_handoff.read_recorded(bundle_dir) == {"longhorn": "failure"}

    world.transport.statuses[c0] = {"flux/longhorn": "success"}
    world.transport.statuses[tip] = {
        "flux/homelab": "success",
        "flux/cluster-base": "success",
        "flux/longhorn": "failure",
    }
    world.transport.status_times[tip] = "2026-10-15T01:02:00Z"
    sent = len(world.transport.requests)
    args = argparse.Namespace(
        target=tip,
        prior=c0,
        bundle_dir=bundle_dir,
        moved_at=MOVED_AT.isoformat(),
        digest="",
    )
    assert vp.run(world.clients(), args, None) == 0
    assert "excused longhorn" in capsys.readouterr().out
    after = [url for _, url in world.transport.requests[sent:]]
    assert not [url for url in after if _status_path(c0) in url]


def test_a_failed_read_of_prods_statuses_is_an_error_and_moves_nothing(
    world, monkeypatch, capsys
):
    c0, *_, tip = world.commits
    world.set_prod(c0, files=_outage("a"))
    world.set_last_good(c0)
    world.publish(tip, files=_outage("b"))
    world.transport.api_forced[_status_path(c0)] = 500
    exit_code, outputs, bundle_dir, err = _decide(world, monkeypatch, capsys, tip)
    assert (exit_code, outputs["verdict"], outputs["target"]) == (1, "error", "")
    assert f"statuses for {c0}: HTTP 500" in err
    assert not bundle_dir.exists()
