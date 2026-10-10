#!/usr/bin/env python3
"""Guard that every `manual —` packageRule rebases its PR only on a conflict (#4011).

A `manual —` group names a half Renovate cannot write: a ledger row, a paired checksum, a
coupled pin. Its PR is red by design until a person pushes that half, so every rebase Renovate
pushes reruns the same red CI. `rebaseWhen: "conflicted"` stops the rebase-on-master-move
pushes. It does not stop the push Renovate makes for a new upstream version.

The top-level `rebaseWhen: "auto"` already acts as `conflicted` for a PR that neither
automerges nor sits on a base branch requiring up-to-date branches. Setting the value on the
rule pins that, so a later strict-checks ruleset or an automerge flip cannot reopen the reruns.

Run: uv run pytest scripts/tests/test_renovate_manual_rules_rebase_on_conflict.py
"""

from _renovate import _PACKAGE_RULES

# Named rather than derived, so a renamed group fails here instead of leaving the census short.
# These two produced 32 of the 268 failed CI runs from 2026-06-07 to 2026-10-09.
MUST_FIND = ("n8n (manual", "code-server build pins (manual")


def _is_manual(rule: dict) -> bool:
    return "(manual" in rule.get("groupName", "")


def _rebases_on_master_move(rule: dict) -> bool:
    return rule.get("rebaseWhen") != "conflicted"


def test_manual_rule_without_rebase_when_is_flagged() -> None:
    rule = {"groupName": "x (manual — finish it)", "automerge": False}
    assert _is_manual(rule) and _rebases_on_master_move(rule)


def test_manual_rule_with_conflicted_is_clean() -> None:
    rule = {"groupName": "x (manual — finish it)", "rebaseWhen": "conflicted"}
    assert _is_manual(rule) and not _rebases_on_master_move(rule)


def test_every_manual_rule_rebases_only_on_conflict() -> None:
    manual = [r for r in _PACKAGE_RULES if _is_manual(r)]
    names = [r["groupName"] for r in manual]
    missing = [m for m in MUST_FIND if not any(n.startswith(m) for n in names)]
    assert not missing, f"the manual-rule census lost {missing}"
    offenders = [r["groupName"] for r in manual if _rebases_on_master_move(r)]
    assert not offenders, (
        f'{len(offenders)} manual rule(s) lack rebaseWhen: "conflicted", so their red PRs '
        f"rerun CI on every rebase: {offenders}"
    )
