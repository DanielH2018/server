#!/usr/bin/env python3
"""Guard that Renovate does not rebase a green automerge PR every time master moves.

Under automerge, the top-level `rebaseWhen: "auto"` resolves to `behind-base-branch`. Renovate
then rebased each automerge branch onto every master move and re-ran its full CI, and inside
the before-6am window master moves faster than one CI run. Both automerge rules set `rebaseWhen: "conflicted"`, so a green PR that is
behind merges as it stands.

A new rule that turns automerge on without the key would undo it silently, so the census
over `automerge: true` rules catches it before any pin resolves through it. The `chased-` rows of
test_renovate_package_rules.py hold the pins that chased master to `conflicted`, and a later rule
that sets `rebaseWhen` back is their red half.

`conflicted` merges a PR that is behind only because master's ruleset does not require
up-to-date branches. That is GitHub state, not repo state, so no test here reads it.

Run: uv run pytest scripts/tests/test_renovate_automerge_does_not_chase_master.py
"""

from _renovate import _PACKAGE_RULES

CONFLICTED = "conflicted"


def _automerge_rules_missing_conflicted(rules: list[dict]) -> list[str]:
    return [
        r.get("groupName") or r["description"][:60]
        for r in rules
        if r.get("automerge") is True and r.get("rebaseWhen") != CONFLICTED
    ]


def test_automerge_rule_without_rebase_when_is_flagged() -> None:
    rule = {"groupName": "x", "automerge": True}
    assert _automerge_rules_missing_conflicted([rule]) == ["x"]


def test_automerge_rule_with_rebase_when_conflicted_is_clean() -> None:
    rule = {"groupName": "x", "automerge": True, "rebaseWhen": CONFLICTED}
    assert _automerge_rules_missing_conflicted([rule]) == []


def test_every_automerge_rule_sets_rebase_when_conflicted() -> None:
    automerging = [r for r in _PACKAGE_RULES if r.get("automerge") is True]
    assert len(automerging) >= 2, (
        "the container-images and k8s digest rules both automerge"
    )
    missing = _automerge_rules_missing_conflicted(_PACKAGE_RULES)
    assert not missing, f"automerge rules without rebaseWhen=conflicted: {missing}"
