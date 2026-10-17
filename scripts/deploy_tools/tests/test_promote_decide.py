"""`promote.py decide`'s verdicts, as `_is_promoted` and `_is_refused` pairs.

Each test runs the CLI against a real throwaway master and the real forge clients over a fake
transport, so a status such as a 500 reaches `decide` through forge's own mapping. Every
refused case asserts its exact verdict and the reason it printed, never only "not promote".

Run: uv run pytest scripts/deploy_tools/tests/test_promote_decide.py
"""

import dataclasses

import pytest

import promote
from _promote_fakes import CONTEXT, World, digest_of


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# The outputs a run that moves nothing writes beside its verdict.
NOTHING_MOVES = {
    "target": "",
    "digest": "",
    "prior": "",
    "prior_digest": "",
    "reset_digest": "",
    "seed": "false",
    "clears_hold": "false",
    "no_reset": "",
    "stateful": "",
    "suspended_hold": "",
}


def _assert_promoted(result, target: str) -> None:
    assert result.exit_code == 0
    assert result.line.endswith(f" verdict=promote target={target}")
    assert {k: result.outputs[k] for k in ("verdict", "target", "digest")} == {
        "verdict": "promote",
        "target": target,
        "digest": digest_of(target),
    }


def _assert_refused(result, verdict: str, reason: str) -> None:
    assert result.exit_code == (1 if verdict == "error" else 0)
    assert result.line.endswith(f" verdict={verdict} target=")
    assert result.outputs == {"verdict": verdict, **NOTHING_MOVES}
    assert reason in result.stderr


# -- promoted ------------------------------------------------------------------------------


def test_a_none_hold_is_promoted(world, monkeypatch, capsys):
    _, c1, c2, tip = world.commits
    world.set_prod(c1)
    world.publish(c2)
    world.publish(tip)
    _assert_promoted(world.decide(monkeypatch, capsys, trigger=tip), tip)
    assert world.run.verified == [digest_of(tip)]


def test_the_first_run_with_no_prod_is_promoted(world, monkeypatch, capsys):
    tip = world.commits[-1]
    world.publish(tip)
    _assert_promoted(world.decide(monkeypatch, capsys, trigger=tip), tip)


def test_an_out_of_order_trigger_is_promoted_to_the_newest_published_commit(
    world, monkeypatch, capsys
):
    """promote(X2) runs after promote(X3) was evicted, and still promotes X3."""
    _, c1, x2, x3 = world.commits
    world.set_prod(c1)
    world.publish(x2)
    world.publish(x3)
    result = world.decide(monkeypatch, capsys, trigger=x2)
    assert result.line == f"trigger={x2} verdict=promote target={x3}"
    _assert_promoted(result, x3)


def test_a_tip_whose_tag_is_not_published_is_skipped_and_the_next_is_promoted(
    world, monkeypatch, capsys
):
    c0, _, c2, tip = world.commits
    world.set_prod(c0)
    world.publish(c2)
    _assert_promoted(world.decide(monkeypatch, capsys, trigger=tip), c2)
    heads = [
        path
        for method, path in world.transport.registry_requests()
        if method == "HEAD" and "/sha-" in path
    ]
    assert [path.rsplit("/", 1)[1] for path in heads] == [f"sha-{tip}", f"sha-{c2}"]


# -- refused -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hold", ["hold-me", "0123456789abcdef0123456789abcdef01234567"]
)
def test_a_hold_other_than_none_is_refused(world, monkeypatch, capsys, hold):
    world.publish(world.commits[-1])
    world.run.hold = hold
    _assert_refused(world.decide(monkeypatch, capsys), "held", f"DEPLOY_HOLD={hold}")
    assert world.transport.requests == [], "a held run reads nothing else"


@pytest.mark.parametrize(
    "hold, fails, reason",
    [
        ("", False, "DEPLOY_HOLD is empty"),
        ("  ", False, "DEPLOY_HOLD is empty"),
        (None, False, "DEPLOY_HOLD does not exist"),
        ("none", True, "Bad credentials"),
    ],
    ids=["empty", "blank", "missing", "read-error"],
)
def test_an_unreadable_or_empty_hold_is_refused(
    world, monkeypatch, capsys, hold, fails, reason
):
    world.publish(world.commits[-1])
    world.run.hold = hold
    world.run.hold_fails = fails
    _assert_refused(world.decide(monkeypatch, capsys), "error", reason)


def test_a_prod_fetch_that_returns_500_is_refused(world, monkeypatch, capsys):
    world.publish(world.commits[-1])
    world.transport.forced["prod"] = 500
    _assert_refused(world.decide(monkeypatch, capsys), "error", "HEAD prod: HTTP 500")


def test_a_prod_manifest_without_the_revision_annotation_is_refused(
    world, monkeypatch, capsys
):
    world.set_prod(world.commits[0], revision="")
    world.publish(world.commits[-1])
    result = world.decide(monkeypatch, capsys)
    _assert_refused(result, "error", promote.REVISION_ANNOTATION)


def test_a_candidate_head_that_returns_500_is_refused_not_skipped(
    world, monkeypatch, capsys
):
    c0, _, c2, tip = world.commits
    world.set_prod(c0)
    world.publish(c2)
    world.transport.forced[f"sha-{tip}"] = 500
    _assert_refused(
        world.decide(monkeypatch, capsys), "error", f"HEAD sha-{tip}: HTTP 500"
    )


@pytest.mark.parametrize(
    "contexts_yaml",
    [
        "gitops_deploy_ci_contexts: []\n",
        'gitops_deploy_ci_contexts: "{{ ci_required_contexts }}"\n',
        "gitops_deploy_ci_contexts:\n  - ''\n",
        "other_key: 1\n",
    ],
    ids=["empty-list", "jinja-string", "empty-name", "missing-key"],
)
def test_a_contexts_list_of_the_wrong_shape_is_refused(
    tmp_path, monkeypatch, capsys, contexts_yaml
):
    world = World(tmp_path, contexts_yaml=contexts_yaml)
    world.publish(world.commits[-1])
    result = world.decide(monkeypatch, capsys)
    _assert_refused(result, "error", "must be a non-empty list of non-empty strings")


def test_a_candidate_lacking_a_required_check_run_is_refused(
    world, monkeypatch, capsys
):
    """A missing required name reads `pending`, as in `ci_verdict`, and is never green."""
    c2, tip = world.commits[2:]
    world.set_prod(c2)
    other = [{"name": "other", "status": "completed", "conclusion": "success"}]
    world.publish(tip, check_runs=other)
    _assert_refused(
        world.decide(monkeypatch, capsys), "current", "published, green and signed"
    )
    assert world.run.verified == [], "an ineligible candidate is never verified"


def test_a_candidate_with_a_failed_check_run_is_refused(world, monkeypatch, capsys):
    c2, tip = world.commits[2:]
    world.set_prod(c2)
    failed = [{"name": CONTEXT, "status": "completed", "conclusion": "failure"}]
    world.publish(tip, check_runs=failed)
    _assert_refused(
        world.decide(monkeypatch, capsys), "current", "published, green and signed"
    )


def test_a_first_run_stops_its_walk_at_the_limit_and_is_refused(
    tmp_path, monkeypatch, capsys
):
    """With no prod, the walk examines the newest FIRST_RUN_WALK_MAX commits and no more.

    Master is one commit longer than the limit, and its root is published and green, so only
    the limit stops the walk from promoting it.
    """
    limit = promote.FIRST_RUN_WALK_MAX
    world = World(tmp_path, size=limit + 1)
    world.publish(world.commits[0])
    _assert_refused(
        world.decide(monkeypatch, capsys),
        "current",
        f"no commit among master's newest {limit}, the first run's limit, is published",
    )
    heads = [
        path
        for method, path in world.transport.registry_requests()
        if method == "HEAD" and "/sha-" in path
    ]
    assert [path.rsplit("/", 1)[1] for path in heads] == [
        f"sha-{sha}" for sha in reversed(world.commits[1:])
    ]


def test_an_ancestry_check_that_exits_128_is_refused(world, monkeypatch, capsys):
    """prod names a commit this checkout lacks, as a shallow clone would."""
    world.set_prod("f" * 40)
    world.publish(world.commits[-1])
    _assert_refused(world.decide(monkeypatch, capsys), "error", "exited 128")


def test_a_prod_outside_masters_history_is_refused(world, monkeypatch, capsys):
    world.set_prod(world.branch_commit())
    world.publish(world.commits[-1])
    _assert_refused(
        world.decide(monkeypatch, capsys), "error", "not in master's history"
    )


def test_a_walk_that_yields_a_non_descendant_is_refused(world):
    """The guard behind the walk: prod never moves backwards, whatever the walk returns."""
    _, c1, c2, _ = world.commits
    world.set_prod(c2)
    world.publish(c1)
    reads = promote.live_reads(world.clients(), world.repo, "master")
    reads = dataclasses.replace(reads, walk=lambda prod, tip: [c1])
    decision = promote.decide(reads)
    assert (decision.verdict, decision.target) == ("error", "")
    assert "does not descend from prod's commit" in decision.reason


def test_a_prod_equal_to_the_tip_is_refused(world, monkeypatch, capsys):
    tip = world.commits[-1]
    world.set_prod(tip)
    world.publish(tip)
    result = world.decide(monkeypatch, capsys, trigger=tip)
    _assert_refused(result, "current", "prod already names master's tip")


def test_a_trigger_older_than_prod_is_refused_with_an_empty_target(
    world, monkeypatch, capsys
):
    _, c1, _, tip = world.commits
    world.set_prod(tip)
    world.publish(c1)
    world.publish(tip)
    result = world.decide(monkeypatch, capsys, trigger=c1)
    assert result.line == f"trigger={c1} verdict=current target="
    _assert_refused(result, "current", "prod already names master's tip")


def test_a_signature_check_that_fails_is_refused_not_skipped(
    world, monkeypatch, capsys
):
    c0, _, c2, tip = world.commits
    world.set_prod(c0)
    world.publish(c2)
    world.publish(tip)
    world.run.cosign_fails = True
    _assert_refused(world.decide(monkeypatch, capsys), "error", "no signatures found")
    assert world.run.verified == [digest_of(tip)], "the walk stops at the failed check"
