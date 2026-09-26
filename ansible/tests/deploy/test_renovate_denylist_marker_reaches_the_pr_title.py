#!/usr/bin/env python3
"""The two denylist marker rules put `k8s_autodeploy: false` in the PR title, not the branch alone.

Issue #2646. Renovate applies a group's `commitMessageTopic` — `{{{groupName}}}`, which is
where the marker lives — only to a branch holding more than one upgrade, and titles a
one-dependency group `Update <dep> …` instead. #2620 was a denied role's pin titled `Update
klutchell/unbound Docker tag to v1.26.1`, with the marker only in its branch slug, and #2258
shows the same shape on a per-package rule. `groupSingleUpdates: true` applies the group
settings to a single upgrade too, so the title carries the marker as well — which is what an
interactive session following the `renovate-prs` triage table reads.

The flag cannot move the branch slug, and that was the risk worth checking before setting it:
Renovate's `generateBranchName` takes the group's `branchTopic` whenever a `groupName` is set,
with no upgrade-count condition, which is why #2620's branch already carried the slugified
marker while its title did not. So no open `manual —` PR is abandoned and reopened, and the
branch tell the `renovate_agent` prompt reads (`k8s_autodeploy-false`, #2641) stays put.

Two classes still arrive bare-titled, so the prompt keeps reading both tells: a PR raised
before this flag landed, and a per-package manual rule on a denied role, which does not carry
the flag. Whether to widen the flag to those rules is #2654.

This guard sits apart from `test_renovate_automerge_follows_the_autodeploy_denylist.py`, which
owns the same two rules, only because that module is at its 500-line cap and
`module_length_allowlist.txt` refuses a tracked file. It imports that module's prefixes and
`find_rule`, so a rename of either rule's marker fails there rather than drifting here.

Run: uv run pytest ansible/tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py
"""

import json

import pytest

from _helpers import REPO
from test_renovate_automerge_follows_the_autodeploy_denylist import (
    DOCKERFILE_MANUAL_GROUP_PREFIX,
    MANUAL_GROUP_PREFIX,
    find_rule,
)

_RENOVATE = REPO / "renovate.json"

TITLE_MARKER_FIELD = "groupSingleUpdates"


def title_marker_problems(rules: list[dict], group_prefix: str) -> list[str]:
    """Every way the rule named by `group_prefix` can leave its title bare. Empty means it does not.

    Pure over the parsed `packageRules` so the red half of the pair below can hand it a rule
    with the field missing, without editing renovate.json.
    """
    _, rule = find_rule(rules, group_prefix)
    if rule.get(TITLE_MARKER_FIELD) is True:
        return []
    return [
        f"the rule grouped {group_prefix!r} does not set `{TITLE_MARKER_FIELD}: true`, so "
        "Renovate titles a one-dependency group `Update <dep> …` and the denylist marker "
        "reaches the branch alone (#2646)"
    ]


def _rules() -> list[dict]:
    return json.loads(_RENOVATE.read_text())["packageRules"]


@pytest.mark.parametrize(
    "group_prefix", [MANUAL_GROUP_PREFIX, DOCKERFILE_MANUAL_GROUP_PREFIX]
)
def test_the_marker_rule_carries_the_title_marker_field(group_prefix: str) -> None:
    problems = title_marker_problems(_rules(), group_prefix)
    assert not problems, "\n".join(problems)


# --- the red-proof pair: the checker must accept a rule with the field and reject one without ---

_RULE_SET = [{"groupName": MANUAL_GROUP_PREFIX + ", so the tick applies nothing)"}]


def test_a_rule_that_sets_the_field_is_clean() -> None:
    rule = _RULE_SET[0] | {TITLE_MARKER_FIELD: True}
    assert title_marker_problems([rule], MANUAL_GROUP_PREFIX) == []


def test_a_rule_that_leaves_the_field_off_is_flagged() -> None:
    problems = title_marker_problems(_RULE_SET, MANUAL_GROUP_PREFIX)
    assert len(problems) == 1
    assert TITLE_MARKER_FIELD in problems[0]
