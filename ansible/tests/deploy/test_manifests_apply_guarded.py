#!/usr/bin/env python3
"""Every reader of `manifests_apply.stdout` outside its own task must tolerate an absent register.

`k8s/manifests` registers `manifests_apply` from the `kubectl apply` command, and five rollout
conditions across four roles read `.stdout` to answer one question: did this run just CREATE the
workload, in which case restarting it races its own initial rollout.

A bare `manifests_apply.stdout` does not merely give a wrong answer when the register is not a
command result — it raises, mid-loop:

    Error while evaluating conditional: object of type 'dict' has no attribute 'stdout'

Seen on a real `deploy.yml --tags observability` run, four times in one play.
Ansible leaves a register set on a task that was skipped, and a skipped task's register is a
plain dict carrying `skipped: true` and no `stdout` — so any path that reaches a consumer
without the apply having produced output turns a deploy red at a task that is not the problem.

`| default('')` makes that case fall through to "not created", which restarts. The two failure
directions are deliberately asymmetric and the safe one was chosen on evidence:

  * a spurious restart races only a first-ever creation — once per workload, and it recovers;
  * a missed restart leaves the pod serving the previous config while the deploy reports green.
    That one was actually observed (the otel collector kept serving a stale config), and is the
    reason these rollout tasks exist at all.

The guard reads each task file PARSED rather than as lines (#3663). The line reading exempted
the ten lines after `register: manifests_apply` as a stand-in for that task's `changed_when`,
so a consumer added within those ten lines passed, and a comment naming the register read as
an offender. A parsed task has the `changed_when` key itself.

Run: uv run pytest ansible/tests/deploy/test_manifests_apply_guarded.py
"""

import re

from lib import yaml_fast
from _helpers import ROLES

REGISTER = "manifests_apply"

# Any `.stdout` access on manifests_apply that is NOT already piped through a default filter.
UNGUARDED = re.compile(r"manifests_apply\.stdout(?!\s*\|\s*default)")
GUARDED = re.compile(r"manifests_apply\.stdout\s*\|\s*default")


def _yaml_sources():
    return sorted(p for p in ROLES.rglob("*.yml") if "archive" not in p.parts)


def _expressions(node, task: str = "<file>", exempt: bool = True):
    """(task name, string) for every string value under `node`, minus one exempt expression.

    The exempt one is the `changed_when` of the task that registers `manifests_apply`: there
    the command has by definition just run, so guarding it would hide a genuine failure rather
    than survive one. `exempt=False` yields that expression too, for the census that proves
    the exemption still has a subject.
    """
    if isinstance(node, dict):
        task = str(node.get("name", task))
        registers = exempt and node.get("register") == REGISTER
        for key, value in node.items():
            if not (registers and key == "changed_when"):
                yield from _expressions(value, task, exempt)
    elif isinstance(node, list):
        for item in node:
            yield from _expressions(item, task, exempt)
    elif isinstance(node, str):
        yield task, node


def unguarded_reads(text: str) -> list[str]:
    """The names of the tasks in one YAML file that read `manifests_apply.stdout` bare."""
    return [
        task
        for doc in yaml_fast.safe_load_all(text)
        for task, expr in _expressions(doc)
        if UNGUARDED.search(expr)
    ]


def test_every_consumer_tolerates_an_absent_register() -> None:
    """No rollout condition may read `.stdout` bare."""
    offenders = [
        f"{path.relative_to(ROLES.parent)}: task {task!r}"
        for path in _yaml_sources()
        for task in unguarded_reads(path.read_text())
    ]
    assert not offenders, (
        "These read `manifests_apply.stdout` without `| default('')`, so a run that reaches "
        "them with the register absent fails the deploy at the wrong task instead of "
        "restarting:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_is_actually_present_somewhere() -> None:
    """Control: the exemption and the guarded reads both still have a subject.

    If the expression were renamed away entirely, the test above would pass vacuously. If the
    registering task stopped reading `.stdout` in its own `changed_when`, the exemption would
    be dead weight that could later hide a real consumer.
    """
    guarded = 0
    exempted = 0
    for path in _yaml_sources():
        for doc in yaml_fast.safe_load_all(path.read_text()):
            guarded += sum(len(GUARDED.findall(e)) for _, e in _expressions(doc))
            every = sum(
                len(UNGUARDED.findall(e)) for _, e in _expressions(doc, exempt=False)
            )
            exempted += every - sum(
                len(UNGUARDED.findall(e)) for _, e in _expressions(doc)
            )
    assert guarded >= 4, (
        f"expected at least the four known rollout consumers to carry the guard, found {guarded} "
        "— if a consumer was removed on purpose, lower this number deliberately"
    )
    assert exempted >= 1, (
        "no task that registers manifests_apply reads its .stdout in its own changed_when, so "
        "the exemption in `_expressions` no longer has a subject: delete it"
    )


def test_a_bare_consumer_is_flagged() -> None:
    assert unguarded_reads(
        "- name: Restart\n  when: \"'created' in manifests_apply.stdout\"\n"
    ) == ["Restart"]


def test_a_bare_read_in_the_registering_task_outside_changed_when_is_flagged() -> None:
    """The exemption is one key of one task, not the whole task the line reading spanned."""
    assert unguarded_reads(
        "- name: Apply\n"
        "  register: manifests_apply\n"
        "  changed_when: \"'created' in manifests_apply.stdout\"\n"
        "  failed_when: \"'error' in manifests_apply.stdout\"\n"
    ) == ["Apply"]


def test_a_guarded_consumer_and_the_own_changed_when_are_clean() -> None:
    assert (
        unguarded_reads(
            "- name: Apply\n"
            "  register: manifests_apply\n"
            "  changed_when: >-\n"
            "    'created' in manifests_apply.stdout\n"
            "# A comment naming manifests_apply.stdout is not an expression.\n"
            "- name: Restart\n"
            "  when: not (manifests_apply.stdout | default('')) is search('created')\n"
        )
        == []
    )
