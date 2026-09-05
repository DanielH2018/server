"""A push to master must land in a concurrency group of its own.

WHY THIS IS A TEST. Every master SHA needs its own CI verdict: the deployer's CI gate and
`await_ci.py` read check-runs per commit, and a commit with no verdict reads `cancelled`
forever, so every landing behind it waits on a later SHA's full sweep instead.

`cancel-in-progress: false` was set on 2026-09-01 to buy that, and it does not. It governs the
RUNNING run and says nothing about a QUEUED one. GitHub keeps at most one pending run per
concurrency group, so when master moves faster than a sweep, each push evicts the pending run
ahead of it. Measured 2026-09-05 with eight sessions landing: nine master runs cancelled
between 16:34:07 and 16:37:31, each 4-52s after creation, every one with zero jobs — evicted
while queued, never started.

That fix read as correct in the file for four days while doing nothing, which is why the
replacement is asserted here rather than only explained in a comment. The property that
matters is not the literal expression: it is that no two master pushes can share a group. An
expression keyed on `github.sha` has that property; one keyed on `github.ref` does not,
because every push to master carries the same ref.

Run: uv run pytest ansible/tests/repo/test_ci_master_runs_are_not_evictable.py
"""

from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _concurrency() -> dict:
    workflow = yaml_fast.safe_load(CI_WORKFLOW.read_text())
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict), (
        "ci.yml declares no concurrency mapping — without one this guard checks nothing"
    )
    return concurrency


def test_the_workflow_still_declares_a_concurrency_group():
    """Non-vacuity: every assertion below reads this key, so its absence must fail loudly."""
    assert "group" in _concurrency()


def test_a_master_push_is_keyed_on_the_commit_not_the_ref():
    """The property: two pushes to master must not collide in one group.

    Every push to master carries ref `refs/heads/master`, so a group keyed on the ref alone
    puts all of them in one group and makes each new push evict the pending one before it.
    Keying the push arm on the SHA gives each commit a group nothing else can enter.
    """
    group = _concurrency()["group"]

    assert "github.sha" in group, (
        f"concurrency.group is {group!r}, which does not key a push on the commit. "
        "Every push to master shares one ref, so they share one group, and GitHub evicts the "
        "pending run whenever a newer push arrives — leaving that master SHA with no CI "
        "verdict and parking every landing behind it."
    )


def test_a_pull_request_still_groups_by_ref_so_a_force_push_supersedes_itself():
    """The other half. Grouping a PR by SHA would leave superseded runs going on a force-push,
    which is what `cancel-in-progress` on the PR arm exists to stop."""
    concurrency = _concurrency()

    assert "github.ref" in concurrency["group"]
    assert "pull_request" in str(concurrency["cancel-in-progress"]), (
        "cancel-in-progress must stay scoped to pull_request: cancelling a master run is the "
        "very thing that leaves a SHA without a verdict."
    )


def test_the_ref_arm_is_conditional_rather_than_unconditional():
    """The reject half — proof this pair can go red.

    The pre-2026-09-05 value, `${{ github.workflow }}-${{ github.ref }}`, satisfies the
    pull-request assertion above on its own. Without this, reverting to it would leave the
    suite green while master went back to losing verdicts.
    """
    group = _concurrency()["group"]

    assert "pull_request" in group, (
        f"concurrency.group is {group!r} — the ref is used unconditionally, so a push to "
        "master is grouped by ref again and is evictable once more."
    )
