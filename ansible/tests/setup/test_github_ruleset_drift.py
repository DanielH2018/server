#!/usr/bin/env python3
"""Run github-ruleset-drift.sh for real against a stubbed GitHub and a recording Kuma push.

Exercised rather than pattern-matched, for the reason the repo keeps relearning: what breaks in
a drift check is its BRANCHING — which conditions report up, which report down, and whether an
unreachable source is distinguishable from a healthy one. A textual guard sees none of that, and
a check that reports "no drift" when it could not look is the exact failure this script exists to
prevent (see a-deadman-is-not-a-failure-report).

Every case below drives the real script through `_ruleset_drift.run`, which repoints only the
Kuma push helper the script sources and `curl`.
"""

import json

import pytest
from _helpers import ANSIBLE
from _ruleset_drift import (
    BRANCH_RULESET_ID,
    DECLARED,
    FENCE_BYPASS,
    FENCE_EXCLUDE,
    FENCE_RULESET_ID,
    RENOVATE_EXCLUDE,
    REVIEW_BYPASS,
    REVIEW_RULESET_ID,
    branch_ruleset_body,
    fence_ruleset,
    review_ruleset,
    ruleset_body,
    run,
)


def test_matching_ruleset_is_clean(tmp_path):
    """The accepting half: live set equals the declared set, enforcement active -> up."""
    rc, status, msg = run(tmp_path, curl_body=ruleset_body(DECLARED))
    assert rc == 0
    assert status == "up"
    assert "matches the declared set" in msg
    assert f"ruleset {REVIEW_RULESET_ID} requires 1 approving review" in msg
    assert f"ruleset {FENCE_RULESET_ID} fences the agent to {FENCE_EXCLUDE[0]}" in msg


def test_a_removed_context_is_flagged(tmp_path):
    """The dangerous direction: a required check dropped in the UI stops gating merges."""
    rc, status, msg = run(tmp_path, curl_body=ruleset_body(DECLARED[:-1]))
    assert rc == 1
    assert status == "down"
    assert "DRIFTED" in msg
    assert "renovate config validator" in msg
    # Named in the no-longer-required half, not the newly-required one.
    assert msg.index("no-longer-required") < msg.index("renovate config validator")


def test_an_added_context_is_flagged(tmp_path):
    """The other direction: something now required that this repo does not declare."""
    rc, status, msg = run(tmp_path, curl_body=ruleset_body([*DECLARED, "new gate"]))
    assert rc == 1
    assert status == "down"
    assert "newly-required" in msg
    assert "new gate" in msg


def test_an_unreachable_api_reports_unverified_not_clean(tmp_path):
    """The failure this check exists to avoid being: silence read as a pass.

    A fetch that never happened must never produce "no drift". It reports DOWN and says the gate
    is UNVERIFIED, which is a different claim from "the gate is wrong".
    """
    rc, status, msg = run(tmp_path, curl_rc=7, curl_body="")
    assert rc == 1
    assert status == "down"
    assert "UNVERIFIED" in msg
    assert "DRIFTED" not in msg


def _journal(tmp_path):
    path = tmp_path / "journal.out"
    return path.read_text().splitlines() if path.exists() else []


def test_a_clean_run_reaches_the_journal(tmp_path):
    """The accepting half of the journal line: a clean run logs its verdict, not only a failed one.

    The push library logs a push only when it fails, so without this line a clean run leaves no
    trace on the host and a verify-by of "confirmed green from its journal" cannot be met.
    """
    run(tmp_path, curl_body=ruleset_body(DECLARED))
    lines = _journal(tmp_path)
    assert len(lines) == 1
    assert lines[0].startswith("status=up ")
    assert "matches the declared set" in lines[0]


def test_a_down_run_reaches_the_journal_with_its_reason(tmp_path):
    """The rejecting half: a DOWN logs the same reason the tile shows."""
    run(tmp_path, curl_body=ruleset_body(DECLARED[1:]))
    lines = _journal(tmp_path)
    assert len(lines) == 1
    assert lines[0].startswith("status=down ")
    assert "DRIFTED" in lines[0]


def test_a_200_that_is_not_a_ruleset_is_a_bad_fetch(tmp_path):
    """A truncated body or an error object must not read as "every check was removed"."""
    rc, status, msg = run(tmp_path, curl_body='{"message":"Not Found"}')
    assert rc == 1
    assert status == "down"
    assert "bad fetch" in msg
    assert "UNVERIFIED" in msg


def test_zero_required_contexts_is_flagged(tmp_path):
    """A well-formed ruleset that requires nothing: every merge gate is open."""
    rc, status, msg = run(tmp_path, curl_body=ruleset_body([]))
    assert rc == 1
    assert status == "down"
    assert "NO status checks" in msg


@pytest.mark.parametrize("enforcement", ["evaluate", "disabled"])
def test_inactive_enforcement_is_flagged(tmp_path, enforcement):
    """Contexts can all be present while the ruleset enforces none of them."""
    rc, status, msg = run(
        tmp_path, curl_body=ruleset_body(DECLARED, enforcement=enforcement)
    )
    assert rc == 1
    assert status == "down"
    assert enforcement in msg


def test_a_missing_renovate_exclusion_is_flagged(tmp_path):
    """The rejecting half of the branch-ruleset arm: merge gate clean, exclusion absent -> down.

    With `renovate/**` under the deletion and non_fast_forward rules, Renovate can neither
    delete a merged branch nor rebase it, so the next PR reuses a SHA that already carries a
    green verdict and automerges empty."""
    rc, status, msg = run(
        tmp_path,
        curl_body=ruleset_body(DECLARED),
        branch_body=branch_ruleset_body(exclude=()),
    )
    assert rc == 1
    assert status == "down"
    assert f"ruleset {BRANCH_RULESET_ID} does not exclude {RENOVATE_EXCLUDE}" in msg


def test_a_narrower_exclusion_is_not_the_exclusion(tmp_path):
    """`refs/heads/renovate/*` is one level; the app's branches nest. Literal match only."""
    rc, status, msg = run(
        tmp_path,
        curl_body=ruleset_body(DECLARED),
        branch_body=branch_ruleset_body(exclude=("refs/heads/renovate/*",)),
    )
    assert rc == 1
    assert status == "down"
    assert "does not exclude" in msg


def test_an_unreachable_branch_ruleset_reports_unverified_not_clean(tmp_path):
    """Merge gate fetched and clean, branch ruleset fetch fails -> down UNVERIFIED, never up."""
    rc, status, msg = run(
        tmp_path,
        curl_body=ruleset_body(DECLARED),
        branch_body="",
    )
    # An empty body parses to no `.enforcement`, which is the bad-fetch branch.
    assert rc == 1
    assert status == "down"
    assert f"ruleset {BRANCH_RULESET_ID}" in msg
    assert "UNVERIFIED" in msg


def _review_rule(**params):
    rule = review_ruleset()["rules"][0]
    return [{**rule, "parameters": {**rule["parameters"], **params}}]


def _actor(actor_type, actor_id, mode="pull_request"):
    return {"actor_id": actor_id, "actor_type": actor_type, "bypass_mode": mode}


ADMIN, RENOVATE = _actor("RepositoryRole", 5), _actor("Integration", 2740)

# Each way the review ruleset can stop requiring a review, and what the DOWN must name.
REVIEW_DRIFT = {
    "switched to evaluate": (
        {"enforcement": "evaluate"},
        ["enforcement is 'evaluate'"],
    ),
    "aimed at another branch": (
        {"conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}}},
        ["no longer targets refs/heads/master"],
    ),
    "review rule removed": ({"rules": []}, ["has no pull_request rule"]),
    "no approval required": (
        {"rules": _review_rule(required_approving_review_count=0)},
        ["required_approving_review_count=0 (declared 1)"],
    ),
    "stale approvals kept": (
        {"rules": _review_rule(dismiss_stale_reviews_on_push=False)},
        ["dismiss_stale_reviews_on_push=false"],
    ),
    "a pusher may approve its own push": (
        {"rules": _review_rule(require_last_push_approval=False)},
        ["require_last_push_approval=false (declared true)"],
    ),
    "the agent's account added": (
        {"bypass_actors": [ADMIN, RENOVATE, _actor("User", 123, "always")]},
        ["bypass actors DRIFTED", "newly-allowed:[User:123:always ]"],
    ),
    "the admin bypass widened to always": (
        {"bypass_actors": [_actor("RepositoryRole", 5, "always"), RENOVATE]},
        [
            "newly-allowed:[RepositoryRole:5:always ]",
            "no-longer-allowed:[RepositoryRole:5:pull_request ]",
        ],
    ),
    "every bypass actor removed": (
        {"bypass_actors": []},
        ["newly-allowed:[]", f"no-longer-allowed:[{' '.join(REVIEW_BYPASS)} ]"],
    ),
    "a body that is not a ruleset": (
        {"enforcement": None},
        ["response had no .enforcement", "UNVERIFIED"],
    ),
    "an anonymous read": (
        {"bypass_actors": None},
        ["no bypass_actors list (null)", "UNVERIFIED"],
    ),
}


@pytest.mark.parametrize("case", sorted(REVIEW_DRIFT))
def test_a_weakened_review_ruleset_is_flagged(tmp_path, case):
    overrides, expected = REVIEW_DRIFT[case]
    review = json.dumps(review_ruleset(**overrides))
    rc, status, msg = run(
        tmp_path, curl_body=ruleset_body(DECLARED), review_body=review
    )
    assert (rc, status) == (1, "down")
    assert f"ruleset {REVIEW_RULESET_ID}" in msg
    for fragment in expected:
        assert fragment in msg


def _fence_conditions(include=("~ALL",), exclude=tuple(FENCE_EXCLUDE)):
    return {"ref_name": {"include": list(include), "exclude": list(exclude)}}


ALWAYS = [_actor("RepositoryRole", 5, "always"), _actor("Integration", 2740, "always")]

# Each way the fence can let the agent's account push outside its prefix.
FENCE_DRIFT = {
    "switched to evaluate": (
        {"enforcement": "evaluate"},
        ["enforcement is 'evaluate'"],
    ),
    "narrowed to master": (
        {"conditions": _fence_conditions(include=["refs/heads/master"])},
        ["no longer covers ~ALL"],
    ),
    "an operator branch opened": (
        {"conditions": _fence_conditions(exclude=[*FENCE_EXCLUDE, "refs/heads/fix-*"])},
        ["exclusions DRIFTED", "newly-open-to-the-agent:[refs/heads/fix-* ]"],
    ),
    "the agent's own prefix dropped": (
        {"conditions": _fence_conditions(exclude=[])},
        [f"no-longer-excluded:[{FENCE_EXCLUDE[0]} ]"],
    ),
    "updates no longer restricted": (
        {"rules": [{"type": "creation"}, {"type": "deletion"}]},
        ["no longer restricts [update ]"],
    ),
    "the agent's account added": (
        {"bypass_actors": [*ALWAYS, _actor("User", 338220904, "always")]},
        ["newly-allowed:[User:338220904:always ]"],
    ),
    "a body that is not a ruleset": (
        {"enforcement": None},
        ["response had no .enforcement", "UNVERIFIED"],
    ),
    "an anonymous read": ({"bypass_actors": None}, ["UNVERIFIED"]),
}


@pytest.mark.parametrize("case", sorted(FENCE_DRIFT))
def test_a_weakened_fence_is_flagged(tmp_path, case):
    overrides, expected = FENCE_DRIFT[case]
    fence = json.dumps(fence_ruleset(**overrides))
    rc, status, msg = run(tmp_path, curl_body=ruleset_body(DECLARED), fence_body=fence)
    assert (rc, status) == (1, "down")
    assert f"ruleset {FENCE_RULESET_ID}" in msg
    for fragment in expected:
        assert fragment in msg


def test_a_bypass_actor_on_the_ci_gate_is_flagged(tmp_path):
    """land.sh's direct merge applies a bypass, so an actor here merges an un-CI'd PR."""
    rc, status, msg = run(
        tmp_path, curl_body=ruleset_body(DECLARED, bypass_actors=[ADMIN])
    )
    assert (rc, status) == (1, "down")
    assert "ruleset 20912512 bypass actors DRIFTED" in msg
    assert "newly-allowed:[RepositoryRole:5:pull_request ]" in msg


def test_an_anonymous_read_of_the_ci_gate_is_unverified(tmp_path):
    """An anonymous read still judges the contexts; it cannot vouch for the bypass list."""
    rc, status, msg = run(
        tmp_path, curl_body=ruleset_body(DECLARED, bypass_actors=None)
    )
    assert (rc, status) == (1, "down")
    assert "ruleset 20912512 returned no bypass_actors list (null)" in msg
    assert "DRIFTED" not in msg


def _defaults():
    import yaml

    return yaml.safe_load(
        (ANSIBLE / "roles/setup/gitops_deploy/defaults/main.yml").read_text()
    )


def test_the_review_ruleset_fixture_matches_the_role_defaults(tmp_path):
    defaults = _defaults()
    assert defaults["gitops_deploy_review_ruleset_id"] == REVIEW_RULESET_ID
    assert defaults["gitops_deploy_review_ruleset_ref"] == "refs/heads/master"
    assert defaults["gitops_deploy_review_ruleset_approvals"] == 1
    assert (
        sorted(defaults["gitops_deploy_review_ruleset_bypass_actors"]) == REVIEW_BYPASS
    )
    assert defaults["gitops_deploy_ruleset_bypass_actors"] == []
    assert defaults["gitops_deploy_fence_ruleset_id"] == FENCE_RULESET_ID
    assert defaults["gitops_deploy_fence_ruleset_exclude"] == FENCE_EXCLUDE
    assert sorted(defaults["gitops_deploy_fence_ruleset_bypass_actors"]) == FENCE_BYPASS


def test_the_branch_ruleset_fixture_matches_the_role_defaults(tmp_path):
    """Same honesty check as below, for the second ruleset's id and pattern."""
    defaults = _defaults()
    assert defaults["gitops_deploy_branch_ruleset_id"] == BRANCH_RULESET_ID
    assert defaults["gitops_deploy_branch_ruleset_renovate_exclude"] == RENOVATE_EXCLUDE


def test_the_declared_set_matches_the_role_defaults(tmp_path):
    """DECLARED above is a fixture; the deployed list lives in defaults. Keep them honest.

    Not a tautology: this is the one assertion that ties the cases above to the real config, so a
    context added to defaults without a thought about the comparison shows up here.
    """
    defaults = _defaults()
    assert defaults["gitops_deploy_expected_ruleset_contexts"] == DECLARED, (
        "gitops_deploy_expected_ruleset_contexts drifted from this test's fixture — if the "
        "ruleset genuinely changed, update both; the monitor compares this list against GitHub"
    )
