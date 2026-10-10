#!/usr/bin/env python3
"""Guard that Renovate pushes to a `manual —` PR at most once a week (#4011).

A `manual —` group names a half Renovate cannot write: a ledger row, a paired checksum, a
coupled pin. Its PR is red by design until a person pushes that half, so every push Renovate
makes reruns the same red CI. Most of those pushes carry a new upstream release, not a rebase:
9 of the 10 retained pushes to the n8n and code-server branches did. So the rule needs three keys.

- `schedule` limits the rule to a Monday window. On its own it gates only branch creation.
- `updateNotScheduled: false` makes Renovate skip updating an existing branch outside that
  window. A ticked rebase box still forces an update, so a person finishing the PR can refresh
  it any day.
- `rebaseWhen: "conflicted"` stops a rebase inside the window just because master moved. The
  top-level `"auto"` behaves this way today only because these rules do not automerge and
  master does not require up-to-date branches.

Vulnerability fixes are unaffected: Renovate applies `vulnerabilityAlerts`, whose schedule is
`at any time`, as a forced rule over every packageRule.

Run: uv run pytest scripts/tests/test_renovate_manual_rules_push_weekly.py
"""

from _renovate import _PACKAGE_RULES

# Named rather than derived, so a renamed group fails here instead of leaving the census short.
# These two produced 32 of the 268 failed CI runs from 2026-06-07 to 2026-10-09.
MUST_FIND = ("n8n (manual", "code-server build pins (manual")

WEEKLY = {
    "schedule": ["before 6am on monday"],
    "updateNotScheduled": False,
    "rebaseWhen": "conflicted",
}


def _is_manual(rule: dict) -> bool:
    return "(manual" in rule.get("groupName", "")


def _missing_keys(rule: dict) -> list[str]:
    return [k for k, v in WEEKLY.items() if rule.get(k) != v]


def test_manual_rule_without_the_weekly_keys_is_flagged() -> None:
    rule = {
        "groupName": "x (manual — finish it)",
        "schedule": ["before 6am on monday"],
        "rebaseWhen": "conflicted",
    }
    assert _is_manual(rule) and _missing_keys(rule) == ["updateNotScheduled"]


def test_manual_rule_with_the_weekly_keys_is_clean() -> None:
    rule = {"groupName": "x (manual — finish it)", **WEEKLY}
    assert _is_manual(rule) and not _missing_keys(rule)


def test_every_manual_rule_pushes_at_most_weekly() -> None:
    manual = [r for r in _PACKAGE_RULES if _is_manual(r)]
    names = [r["groupName"] for r in manual]
    missing = [m for m in MUST_FIND if not any(n.startswith(m) for n in names)]
    assert not missing, f"the manual-rule census lost {missing}"
    offenders = {r["groupName"]: _missing_keys(r) for r in manual if _missing_keys(r)}
    assert not offenders, (
        f"{len(offenders)} manual rule(s) let Renovate push to a red PR more than weekly, "
        f"rerunning its CI each time: {offenders}"
    )
