#!/usr/bin/env python3
"""Guard that Renovate does not rebase a green automerge PR every time master moves (#5587).

Under automerge, the top-level `rebaseWhen: "auto"` resolves to `behind-base-branch`. Renovate
then rebased each automerge branch onto every master move and re-ran its full CI, and inside
the before-6am window master moves faster than one CI run. From 2026-10-16 to 2026-10-18 the
automerge branches drew 77 PR runs and 1,389 runner-minutes; the three named below ran CI 15,
14 and 13 times. Both automerge rules set `rebaseWhen: "conflicted"`, so a green PR that is
behind merges as it stands.

Two things can undo it silently, so each gets an assertion:

- A later rule that matches these pins and sets `rebaseWhen` back. `_resolve_setting` walks
  the rules in order, so the resolved value, not the rule's own body, is asserted.
- A new rule that turns automerge on without the key. The census over `automerge: true`
  rules catches it before any pin resolves through it.

`conflicted` merges a PR that is behind only because master's ruleset does not require
up-to-date branches. That is GitHub state, not repo state, so no test here reads it.

Run: uv run pytest scripts/tests/test_renovate_automerge_does_not_chase_master.py
"""

import pytest
from _renovate import _PACKAGE_RULES, _REPO, _resolve_setting

# The branches #5587 measured, as package -> (file carrying the pin, the update type the
# branch carried). Named rather than derived, so a moved pin fails as a missing member instead
# of resolving against a path no rule was written for.
CHASED = {
    "nginxinc/nginx-unprivileged": (
        "ansible/roles/k8s/docs/defaults/main.yml",
        "digest",
    ),
    "lscr.io/linuxserver/radarr": (
        "ansible/roles/k8s/radarr/defaults/main.yml",
        "patch",
    ),
    "ghcr.io/headlamp-k8s/headlamp": (
        "ansible/roles/k8s/headlamp/defaults/main.yml",
        "minor",
    ),
}

CONFLICTED = "conflicted"


def _resolve(field: str, package: str, rules=_PACKAGE_RULES):
    rel_path, update_type = CHASED[package]
    return _resolve_setting(
        field, package, rel_path, update_type, "docker", rules=rules
    )


@pytest.mark.parametrize("package", sorted(CHASED))
def test_each_chased_pin_still_sits_where_the_rule_looks(package: str) -> None:
    rel_path, _ = CHASED[package]
    assert f"_image: {package}:" in (_REPO / rel_path).read_text(), (
        f"{rel_path} no longer pins {package} — pick another automerge pin, or this test "
        "proves nothing about the automerge rules"
    )


@pytest.mark.parametrize("package", sorted(CHASED))
def test_chased_pin_automerges_without_rebasing_onto_master(package: str) -> None:
    assert _resolve("automerge", package) is True, (
        f"{package} no longer automerges, so it no longer exercises the rebase setting"
    )
    assert _resolve("rebaseWhen", package) == CONFLICTED, (
        f"{package} automerges with rebaseWhen={_resolve('rebaseWhen', package)!r}: Renovate "
        "rebases it onto every master move and re-runs its CI until master goes quiet"
    )


def _automerge_rules_missing_conflicted(rules: list[dict]) -> list[str]:
    return [
        r.get("groupName") or r["description"][:60]
        for r in rules
        if r.get("automerge") is True and r.get("rebaseWhen") != CONFLICTED
    ]


def test_later_rule_resetting_rebase_when_is_flagged() -> None:
    rules = [*_PACKAGE_RULES, {"matchManagers": ["custom.regex"], "rebaseWhen": "auto"}]
    assert _resolve("rebaseWhen", "lscr.io/linuxserver/radarr", rules=rules) == "auto"


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
