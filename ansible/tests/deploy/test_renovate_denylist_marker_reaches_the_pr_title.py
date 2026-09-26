#!/usr/bin/env python3
"""Every rule carrying the denylist marker puts `k8s_autodeploy: false` in the PR title, not the branch alone.

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

#2654 widened the flag from the two denylist rules to every rule whose `groupName` carries the
marker: the per-package manual rules (meilisearch, the CrowdSec bouncer plugin, n8n and the
rest) group one dependency most of the time too. Their title then reads `Update <group name> to
<version>`, so the manual work order in front of the marker reaches the title as well. A PR
raised before the flag still arrives bare-titled, so the prompt keeps reading both tells.

This guard sits apart from `test_renovate_automerge_follows_the_autodeploy_denylist.py`, which
owns the two denylist rules, only because that module is at its 500-line cap and
`module_length_allowlist.txt` refuses a tracked file. It imports that module's prefixes, so a
rename of either rule's marker fails there rather than drifting here.

Run: uv run pytest ansible/tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py
"""

import json

from _helpers import REPO
from test_renovate_automerge_follows_the_autodeploy_denylist import (
    DOCKERFILE_MANUAL_GROUP_PREFIX,
    MANUAL_GROUP_PREFIX,
)

_RENOVATE = REPO / "renovate.json"

TITLE_MARKER_FIELD = "groupSingleUpdates"
MARKER = "k8s_autodeploy: false"

# The rules the census must find, by groupName head, so a rule renamed or stripped of its
# marker fails as a missing member rather than passing over a smaller set.
KNOWN_MARKER_RULE_PREFIXES = frozenset(
    {
        MANUAL_GROUP_PREFIX,
        DOCKERFILE_MANUAL_GROUP_PREFIX,
        "meilisearch (manual",
        "crowdsec bouncer plugin (manual",
        "karakeep time-tagger pip deps (manual",
        "code-server build pins (manual",
        "n8n (manual",
        "n8n fuzzball (manual",
    }
)


def marker_rules(rules: list[dict]) -> list[dict]:
    """Every rule whose `groupName` carries the denylist marker."""
    return [r for r in rules if MARKER in r.get("groupName", "")]


def title_marker_problems(rules: list[dict]) -> list[str]:
    """Every marker rule that can leave its title bare. Empty means none does.

    Pure over the parsed `packageRules` so the red half of the pair below can hand it a rule
    with the field missing, without editing renovate.json.
    """
    return [
        f"the rule grouped {rule['groupName']!r} does not set `{TITLE_MARKER_FIELD}: true`, "
        "so Renovate titles a one-dependency group `Update <dep> …` and the denylist marker "
        "reaches the branch alone (#2646, #2654)"
        for rule in marker_rules(rules)
        if rule.get(TITLE_MARKER_FIELD) is not True
    ]


def _rules() -> list[dict]:
    return json.loads(_RENOVATE.read_text())["packageRules"]


def test_the_census_finds_every_known_marker_rule() -> None:
    names = [r["groupName"] for r in marker_rules(_rules())]
    missing = sorted(
        p for p in KNOWN_MARKER_RULE_PREFIXES if not any(n.startswith(p) for n in names)
    )
    assert not missing, f"no marker rule's groupName starts with: {missing}"


def test_every_marker_rule_carries_the_title_marker_field() -> None:
    problems = title_marker_problems(_rules())
    assert not problems, "\n".join(problems)


# --- the red-proof pair: the checker must accept a rule with the field and reject one without ---

_RULE = {
    "groupName": "meilisearch (manual upgrade — DB format migration; k8s_autodeploy: false)"
}


def test_a_marker_rule_that_sets_the_field_is_clean() -> None:
    assert title_marker_problems([_RULE | {TITLE_MARKER_FIELD: True}]) == []


def test_a_marker_rule_that_leaves_the_field_off_is_flagged() -> None:
    problems = title_marker_problems([_RULE, {"groupName": "prek hooks"}])
    assert len(problems) == 1
    assert TITLE_MARKER_FIELD in problems[0]
