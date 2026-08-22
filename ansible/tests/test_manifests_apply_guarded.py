#!/usr/bin/env python3
"""Every reader of `manifests_apply.stdout` outside its own task must tolerate an absent register.

`k8s/manifests` registers `manifests_apply` from the `kubectl apply` command, and five rollout
conditions across four roles read `.stdout` to answer one question: did this run just CREATE the
workload, in which case restarting it races its own initial rollout.

A bare `manifests_apply.stdout` does not merely give a wrong answer when the register is not a
command result — it raises, mid-loop:

    Error while evaluating conditional: object of type 'dict' has no attribute 'stdout'

Observed on a real `deploy.yml --tags claude-otel` run on 2026-08-22, four times in one play.
Ansible leaves a register set on a task that was skipped, and a skipped task's register is a
plain dict carrying `skipped: true` and no `stdout` — so any path that reaches a consumer
without the apply having produced output turns a deploy red at a task that is not the problem.

`| default('')` makes that case fall through to "not created", which restarts. The two failure
directions are deliberately asymmetric and the safe one was chosen on evidence:

  * a spurious restart races only a first-ever creation — once per workload, and it recovers;
  * a missed restart leaves the pod serving the previous config while the deploy reports green.
    That one was actually observed (the otel collector kept serving a stale config), and is the
    reason these rollout tasks exist at all.

Run: uv run pytest ansible/tests/test_manifests_apply_guarded.py
"""

import re
from pathlib import Path

ROLES = Path(__file__).resolve().parents[1] / "roles"

# The task that registers it evaluates `.stdout` in its own changed_when, where the command has
# by definition just run. Guarding that one would hide a genuine failure rather than survive one.
REGISTERING_ROLE = ROLES / "k8s" / "manifests" / "tasks" / "main.yml"

# Any `.stdout` access on manifests_apply that is NOT already piped through a default filter.
UNGUARDED = re.compile(r"manifests_apply\.stdout(?!\s*\|\s*default)")


def _yaml_sources():
    return sorted(p for p in ROLES.rglob("*.yml") if "archive" not in p.parts)


def _own_changed_when_lines(text: str) -> range:
    """Line numbers belonging to the registering task's own `changed_when` expression.

    That expression is the one legitimate bare read — the command has by definition just run,
    so guarding it would hide a genuine failure instead of surviving one. It is a folded block
    spanning several lines, so this exempts the block rather than a single line.
    """
    for i, line in enumerate(text.splitlines()):
        if line.strip() == "register: manifests_apply":
            return range(i + 1, i + 11)
    raise AssertionError("k8s/manifests no longer registers manifests_apply")


def test_every_consumer_tolerates_an_absent_register() -> None:
    """No rollout condition may read `.stdout` bare."""
    offenders = []
    for path in _yaml_sources():
        text = path.read_text()
        exempt = _own_changed_when_lines(text) if path == REGISTERING_ROLE else range(0)
        for num, line in enumerate(text.splitlines(), 1):
            if not UNGUARDED.search(line) or num in exempt:
                continue
            offenders.append(f"{path.relative_to(ROLES.parent)}:{num}: {line.strip()}")

    assert not offenders, (
        "These read `manifests_apply.stdout` without `| default('')`, so a run that reaches "
        "them with the register absent fails the deploy at the wrong task instead of "
        "restarting:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_is_actually_present_somewhere() -> None:
    """Control: if the expression were renamed away entirely, the test above passes vacuously."""
    guarded = sum(
        len(re.findall(r"manifests_apply\.stdout\s*\|\s*default", p.read_text()))
        for p in _yaml_sources()
    )
    assert guarded >= 5, (
        f"expected at least the five known rollout consumers to carry the guard, found {guarded} "
        "— if a consumer was removed on purpose, lower this number deliberately"
    )
