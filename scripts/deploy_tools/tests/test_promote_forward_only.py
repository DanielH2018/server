"""H1.1b's forward-only rules in `promote.py decide`: the no-reset names, the gate, the backstop.

The cases run the CLI against a real throwaway master whose commits carry role defaults and
pin files, over the fake forge in `_promote_fakes.py`. `test_promote_settle.py` holds the
`settle` half, which writes the hold. The plan's *Verification* cases for H1.1b are in
docs/target-architecture/host/h1-cluster-config-to-flux.md.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_forward_only.py
"""

from datetime import timedelta

import pytest
from _promote_fakes import NOW, World, bundle_files, digest_of

from deploy_tools.promote_lib import forward_only, verify_promotion as vp

PINS = "ansible/roles/setup/k3s/defaults/main.yml"
FLAGGED = "flux_forward_only: true\n"
LONGHORN = "ansible/roles/k8s/longhorn/defaults/main.yml"
METALLB = "ansible/roles/k8s/metallb/defaults/main.yml"
BENTO = "ansible/roles/k8s/bento-pdf/defaults/main.yml"
GATES = forward_only.GATE_STATUS_CONTEXT


def _pins(longhorn: str = "v1.12.1", metallb: str = "v0.15.2") -> str:
    return f"k3s_longhorn_version: {longhorn}\nk3s_metallb_version: {metallb}\n"


def _bundle(**footprints: str) -> dict[str, bytes]:
    body = {
        n.replace("_", "-"): {"own": v, "footprint": v} for n, v in footprints.items()
    }
    body["_unclassified"] = {"own": "u", "footprint": "u"}
    return bundle_files(body)


def _base(world) -> str:
    """The commit after master's root that adds the pins and the flagged role defaults."""
    return world.commits[1]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, size=1)
    w.commit_files(
        "base", {PINS: _pins(), LONGHORN: FLAGGED, METALLB: FLAGGED, BENTO: "x: 1\n"}
    )
    return w


def _settled_at(world, sha: str, files: dict[str, bytes] | None = None) -> None:
    """`prod` and `last-good` both name `sha`, whose bundle holds `files`."""
    world.set_prod(sha, files=files)
    world.set_last_good(sha)


def _gated(world, sha: str, age: timedelta, state: str = "success") -> None:
    """Map `sha` to a merged PR whose head carries a gate status `age` old."""
    head = f"{sha[:8]}{'f' * 32}"
    world.transport.pulls[sha] = [
        {"number": 7, "merge_commit_sha": sha, "head": {"sha": head}}
    ]
    world.transport.statuses[head] = {GATES: state}
    world.transport.status_times[head] = (NOW - age).isoformat()


def _decide(world, monkeypatch, capsys):
    return world.decide(monkeypatch, capsys, trigger=world.commits[-1])


# -- the hold without a reset: what `decide` names ------------------------------------------


def test_a_revision_that_changes_longhorn_is_named_for_a_hold_without_reset(
    world, monkeypatch, capsys
):
    _settled_at(world, _base(world), _bundle(longhorn="a", bento_pdf="b"))
    t = world.commit("t")
    world.publish(t, files=_bundle(longhorn="a2", bento_pdf="b"))
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "promote", result.stderr
    assert result.outputs["no_reset"] == "longhorn"


def test_a_revision_whose_last_good_lacks_longhorn_is_named(world, monkeypatch, capsys):
    _settled_at(world, _base(world), _bundle(bento_pdf="b"))
    t = world.commit("t")
    world.publish(t, files=_bundle(longhorn="a", bento_pdf="b"))
    assert _decide(world, monkeypatch, capsys).outputs["no_reset"] == "longhorn"


def test_a_revision_that_changes_only_bento_pdf_names_nothing_and_resets(
    world, monkeypatch, capsys
):
    _settled_at(world, _base(world), _bundle(longhorn="a", metallb="m", bento_pdf="b"))
    t = world.commit("t")
    world.publish(t, files=_bundle(longhorn="a", metallb="m", bento_pdf="b2"))
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "promote", result.stderr
    assert result.outputs["no_reset"] == ""


def test_a_revision_that_drops_metallb_is_named_though_t_has_no_metallb_role(
    world, monkeypatch, capsys
):
    _settled_at(world, _base(world), _bundle(longhorn="a", metallb="m"))
    t = world.commit_files("drop metallb", {METALLB: None})
    world.publish(t, files=_bundle(longhorn="a"))
    assert _decide(world, monkeypatch, capsys).outputs["no_reset"] == "metallb"


def test_unreadable_digests_count_every_forward_only_tag_as_changed():
    flagged = frozenset({"longhorn", "metallb"})
    assert forward_only.no_reset_names(None, flagged) == flagged
    unclassified = forward_only.changed_tags(
        {"_unclassified": {"footprint": "u2"}, "bento-pdf": {"footprint": "b"}},
        {"_unclassified": {"footprint": "u"}, "longhorn": {"footprint": "a"}},
    )
    assert unclassified == {"bento-pdf", "longhorn"}


# -- the gate ---------------------------------------------------------------------------------


def _assert_held(world, result, reason: str) -> None:
    t = world.commits[-1]
    assert result.outputs["verdict"] == "held", result.stderr
    assert result.outputs["target"] == ""
    assert reason in result.stderr
    assert world.run.written == [("DEPLOY_HOLD", t)]


def test_a_bump_with_no_gate_status_holds_and_writes_t_alone(
    world, monkeypatch, capsys
):
    _settled_at(world, _base(world))
    t = world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    world.publish(t)
    world.transport.pulls[t] = [
        {"number": 7, "merge_commit_sha": t, "head": {"sha": "e" * 40}}
    ]
    _assert_held(world, _decide(world, monkeypatch, capsys), f"no {GATES} status")


def test_a_gated_bump_then_an_unrelated_commit_promotes_to_the_unrelated_commit(
    world, monkeypatch, capsys
):
    _settled_at(world, _base(world))
    bump = world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    _gated(world, bump, timedelta(hours=1))
    t = world.commit("another session's PR")
    world.publish(t)
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "promote", result.stderr
    assert result.outputs["target"] == t
    assert world.run.written == []


def test_the_same_range_with_an_ungated_bump_holds(world, monkeypatch, capsys):
    _settled_at(world, _base(world))
    world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    world.publish(world.commit("another session's PR"))
    _assert_held(world, _decide(world, monkeypatch, capsys), "maps to no merged PR")


def test_a_bump_whose_only_pr_merged_as_another_commit_holds(
    world, monkeypatch, capsys
):
    """A direct push that a later PR's branch also carries is not that PR's merge."""
    _settled_at(world, _base(world))
    t = world.commit_files("pushed", {PINS: _pins(longhorn="v1.13.0")})
    _gated(world, t, timedelta(hours=1))
    world.transport.pulls[t][0]["merge_commit_sha"] = "c" * 40
    world.publish(t)
    _assert_held(world, _decide(world, monkeypatch, capsys), "maps to no merged PR")


def test_a_bump_whose_pr_lookup_fails_is_an_error(world, monkeypatch, capsys):
    _settled_at(world, _base(world))
    t = world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    world.publish(t)
    world.transport.api_forced[f"/repos/DanielH2018/server/commits/{t}/pulls"] = 500
    result = _decide(world, monkeypatch, capsys)
    assert result.exit_code == 1
    assert result.outputs["verdict"] == "error"
    assert "pull requests for" in result.stderr
    assert world.run.written == []


def test_a_hold_write_that_fails_is_an_error(world, monkeypatch, capsys):
    _settled_at(world, _base(world))
    t = world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    world.publish(t)
    world.run.set_fails = True
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "error"
    assert "writing DEPLOY_HOLD failed" in result.stderr


@pytest.mark.parametrize(
    ("age", "verdict"), [(timedelta(hours=7), "held"), (timedelta(hours=1), "promote")]
)
def test_a_seven_hour_old_status_holds_and_a_one_hour_old_one_promotes(
    world, monkeypatch, capsys, age, verdict
):
    _settled_at(world, _base(world))
    t = world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    _gated(world, t, age)
    world.publish(t)
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == verdict, result.stderr
    if verdict == "held":
        _assert_held(world, result, "over the 6h bound")


def test_a_failed_gate_status_holds(world, monkeypatch, capsys):
    _settled_at(world, _base(world))
    t = world.commit_files("bump", {PINS: _pins(longhorn="v1.13.0")})
    _gated(world, t, timedelta(hours=1), state="failure")
    world.publish(t)
    _assert_held(world, _decide(world, monkeypatch, capsys), f"{GATES}=failure")


# -- the backstop -----------------------------------------------------------------------------


def test_two_gated_bumps_of_one_pin_in_prod_to_t_hold(world, monkeypatch, capsys):
    _settled_at(world, _base(world))
    first = world.commit_files("bump 1", {PINS: _pins(longhorn="v1.13.0")})
    second = world.commit_files("bump 2", {PINS: _pins(longhorn="v1.13.1")})
    for sha in (first, second):
        _gated(world, sha, timedelta(hours=1))
    world.publish(second)
    _assert_held(world, _decide(world, monkeypatch, capsys), "in 2 commits")


def test_one_gated_commit_that_skips_a_longhorn_minor_holds(world, monkeypatch, capsys):
    _settled_at(world, _base(world))
    t = world.commit_files("bump", {PINS: _pins(longhorn="v1.14.0")})
    _gated(world, t, timedelta(hours=1))
    world.publish(t)
    _assert_held(
        world, _decide(world, monkeypatch, capsys), "more than one minor release"
    )


def test_after_a_cleared_hold_on_a_failed_bump_one_more_gated_bump_promotes(
    world, monkeypatch, capsys
):
    """`prod` sits on the failed bump under the `held` tag, `last-good` before it, and the
    operator has cleared DEPLOY_HOLD. The failed bump's status is seven hours old, which does
    not hold, because that commit is at or below `prod`."""
    world.store(digest_of(_base(world)))
    world.set_last_good(_base(world))
    failed = world.commit_files("bump 1", {PINS: _pins(longhorn="v1.13.0")})
    _gated(world, failed, timedelta(hours=7))
    world.set_prod(failed)
    world.transport.tags["held"] = digest_of(failed)
    fix = world.commit_files("bump 2", {PINS: _pins(longhorn="v1.14.0")})
    _gated(world, fix, timedelta(hours=1))
    world.publish(fix)
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "promote", result.stderr
    assert result.outputs["target"] == fix
    assert result.outputs["reset_digest"] == digest_of(_base(world))


# -- the in-flight wait ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "stop"), [("sha256:p", None), ("sha256:other", "current")]
)
def test_a_prod_under_the_held_tag_is_settled_and_any_other_difference_waits(
    held, stop
):
    tags = {"prod": "sha256:p", "last-good": "sha256:lg", "held": held}
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    settled = vp.wait_settled(
        lambda: "none", tags.get, sleep, lambda: clock[0], revert_rule=False
    )
    assert (settled.stop[0] if settled.stop else None) == stop
