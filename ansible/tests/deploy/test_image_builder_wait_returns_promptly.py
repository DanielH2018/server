#!/usr/bin/env python3
"""Guards that k8s/image-builder's build wait ends as soon as the Job reaches EITHER outcome.

`kubectl wait` takes one condition per invocation, and a failed Job never gets
`condition=complete`. The role used to sequence the two waits with `||`, which reads as a race
and is a fallback: the failed-wait could not start until the complete-wait had already burned
the full `image_builder_timeout`. Measured 2026-09-10 on code-server (`image_builder_timeout:
1800`), a Job that reached BackoffLimitExceeded 6s in still had the deploy blocked on it ~17
minutes later, and would have sat there ~30 minutes (#1535).

The replacement is an `until:` poll naming both condition types, the shape k8s/cronjob-gate
already uses. These tests evaluate that `until` through Ansible's own templar against the
stdout each Job state produces, rather than asserting its text — a reworded expression that
still stops on `Failed` must pass, and a rewritten one that does not must fail.

Three properties, the first two being the red-proof pair:

1. A Job reporting Complete OR Failed satisfies the poll on the attempt that observes it. The
   `Failed` half is the whole change: it is what a one-sided wait cannot see.
2. A Job reporting neither does NOT satisfy it. Without this half, an `until` that were simply
   true would pass property 1 while never waiting for a build at all.
3. The retries and delay still span `image_builder_timeout`, so shortening the wait did not
   quietly shorten the deadline a slow first build depends on.

Run: uv run pytest ansible/tests/deploy/test_image_builder_wait_returns_promptly.py
"""

import pytest
from ansible.template import Templar, trust_as_template
from _helpers import ANSIBLE
from _helpers import load_tasks


TASKS = ANSIBLE / "roles" / "k8s" / "image-builder" / "tasks" / "main.yml"

WAIT = "Wait for the build to finish"

# What `-o jsonpath={.status.conditions[*].type}` prints, read off live build Jobs on
# 2026-09-10: a completed one carries SuccessCriteriaMet beside Complete.
COMPLETE = "SuccessCriteriaMet Complete"
FAILED = "Failed"
RUNNING = ""


def _wait_task() -> dict:
    matches = [t for t in load_tasks(TASKS) if t.get("name", "").startswith(WAIT)]
    assert len(matches) == 1, f"{WAIT!r} matched {len(matches)} tasks in {TASKS}"
    return matches[0]


def _poll_satisfied(stdout: str) -> bool:
    """Would the task's own `until:` stop the poll on a Job whose conditions are `stdout`?"""
    task = _wait_task()
    until = task.get("until")
    assert until, (
        f"{WAIT!r} has no `until:`. It is back to waiting on one condition, so a failed build "
        "blocks the deploy for the whole image_builder_timeout — that is #1535."
    )
    register = task["register"]
    templar = Templar(
        loader=None,
        variables={register: {"stdout": stdout, "rc": 0}},
    )
    return bool(templar.template(trust_as_template("{{ " + str(until) + " }}")))


@pytest.mark.parametrize(
    ("conditions", "state"),
    [
        pytest.param(COMPLETE, "complete", id="complete"),
        pytest.param(FAILED, "failed", id="failed"),
    ],
)
def test_either_terminal_condition_ends_the_poll(conditions, state):
    """Both outcomes end the wait, and the FAILED one is the whole point of the change.

    A failed build used to block the deploy for the full timeout — 15 minutes at the role
    default, 30 at code-server's — while the Job had said so within seconds.
    """
    assert _poll_satisfied(conditions), (
        f"a Job whose conditions are {conditions!r} does not satisfy the poll, so a {state} "
        "build keeps the deploy waiting until image_builder_timeout is spent."
    )


def test_an_unfinished_job_keeps_the_poll_running():
    """The other direction: a poll that stopped on anything would never wait for a build."""
    assert not _poll_satisfied(RUNNING), (
        "a Job carrying no terminal condition satisfies the poll, so the wait returns while "
        "the build is still running and the assert below it reads an empty result."
    )


def test_the_poll_still_spans_the_configured_timeout():
    """Retries x delay must still cover image_builder_timeout, at the default and above it.

    Rendered from the role rather than restated, so a hardcoded retry count — which would cap
    every caller at one budget and silently shorten code-server's 1800s — fails here.
    """
    task = _wait_task()
    delay = int(task["delay"])
    for timeout in (900, 1800):
        templar = Templar(loader=None, variables={"image_builder_timeout": timeout})
        retries = int(templar.template(trust_as_template(str(task["retries"]))))
        assert retries * delay >= timeout, (
            f"the poll gives up after {retries * delay}s with image_builder_timeout={timeout}. "
            "A first build on a cold cache is then reported as a failure it never had."
        )


def test_the_poll_reads_the_job_rather_than_reporting_its_own_rc():
    """A poll over something that observes nothing gates nothing.

    `until:` is loop control available to any module, so the check is that this one runs a
    kubectl read of the Job it is waiting on.
    """
    cmd = str(_wait_task()["ansible.builtin.command"]["cmd"])
    assert "get job build-" in cmd and "status.conditions" in cmd, (
        f"the wait no longer reads the build Job's conditions; it runs {cmd!r}."
    )
