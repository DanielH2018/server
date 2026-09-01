"""The storage smoke check must be able to report a red.

`tasks/storage_smoke.yml` binds a Longhorn PVC to prove the cluster can provision one. Its
wait task carries `failed_when: false` so that a read exiting non-zero is retried rather than
failing on the first attempt — which means the ONLY things standing between it and a check
that can never fail are the `until` and the assert that follows it.

Drop the `until` and the task passes on a Pending PVC. Drop the assert and an exhausted
`until` still fails, but says "conditions not met" without naming the phase, which is the
difference between "Longhorn cannot provision" and "the PVC never appeared". Either edit
reads as a tidy-up.

This repo has paid for a check that was only ever observed passing twice — volume-claim's
short-circuit fired for 0 of 25 claims behind 16 green tests, and image-smoke's bare-boot rule
caught nothing across 11 failures. So the rule is that a new check ships with a proof it can
go red. For an Ansible task file against a live cluster the red-proof cannot be a unit test of
the verdict, so it is this: the structure that makes a red reachable is asserted here, and a
later edit that removes it fails the suite rather than going quiet.
"""

import pytest
import yaml

from _helpers import ANSIBLE, ROLES

SMOKE = ROLES / "setup" / "k3s" / "tasks" / "storage_smoke.yml"
WAIT_TASK = "Wait for the PVC to reach Bound"
ASSERT_TASK = "Report the phase the PVC actually reached"
DELETE_TASK = "Remove the storage smoke PVC"


def _tasks():
    return yaml.safe_load(SMOKE.read_text())


def _block_task():
    """The task carrying the block/always pair, whatever it is named."""
    for task in _tasks():
        if "block" in task:
            return task
    pytest.fail(
        f"no block/always task found in {SMOKE} — the cleanup structure is gone."
    )


def _named(tasks, name):
    for task in tasks:
        if task.get("name") == name:
            return task
    return None


def test_the_wait_is_bounded_by_an_until_on_bound():
    """Without this, `failed_when: false` makes the wait unfailable."""
    wait = _named(_block_task()["block"], WAIT_TASK)
    assert wait, f"{SMOKE} has no task named {WAIT_TASK!r}."
    until = str(wait.get("until", ""))
    assert "Bound" in until, (
        f"the wait task in {SMOKE} has until={until!r}. It carries `failed_when: false` so a "
        f"transient read is retried, which means the `until` is the only thing that can fail "
        f"it. Without a Bound comparison this check passes on a Pending PVC."
    )


def test_the_phase_is_asserted_after_the_wait():
    """An exhausted `until` fails without naming the phase; the assert is what names it."""
    block = _block_task()["block"]
    check = _named(block, ASSERT_TASK)
    assert check, (
        f"{SMOKE} has no task named {ASSERT_TASK!r}. The wait's `failed_when: false` means an "
        f"assert on the same register is what turns 'conditions not met' into a phase."
    )
    that = str(
        (check.get("ansible.builtin.assert") or check.get("assert") or {}).get(
            "that", ""
        )
    )
    assert "Bound" in that, (
        f"the assert in {SMOKE} tests {that!r}, which does not compare against Bound."
    )


def test_the_cleanup_runs_in_an_always():
    """A failed bind must not leave a Pending PVC holding the name the next run needs."""
    always = _block_task().get("always")
    assert always, (
        f"{SMOKE} has no `always:` section. The PVC delete must run there — in `block:` it is "
        f"skipped by the very failure that leaves the PVC behind."
    )
    assert _named(always, DELETE_TASK), (
        f"{SMOKE} has an `always:` that does not contain {DELETE_TASK!r}. Cleanup that only "
        f"runs on success is not cleanup."
    )


def test_the_check_is_not_wired_into_the_bringup_role():
    """Opt-in by construction: prod must not create and destroy a PVC on every k3s run."""
    main = (ROLES / "setup" / "k3s" / "tasks" / "main.yml").read_text()
    assert "storage_smoke" not in main, (
        "storage_smoke.yml is imported from tasks/main.yml, so it now runs on every k3s-role "
        "run including prod's. It is reached through its own playbook "
        "(ansible/k3s-storage-smoke.yml -e storage_smoke=<host>), which keeps prod free of "
        "the churn."
    )


def test_the_check_has_its_own_playbook_and_is_not_folded_into_the_bringup():
    """k3s-bringup.yml cannot host this play, and the reason is not stylistic.

    Its first play takes `hosts: {{ target | default(lookup('pipe','hostname')) }}` and
    asserts the resulting host is in `k3s_server_hosts` under `tags: always`. Any play
    appended there therefore inherits "the host you invoke from must itself be a k3s server"
    — and the only host that can reach daniel-stage is daniel-server, which deliberately is
    not one. Folding it back reads as tidying and breaks the single caller.
    """
    own = ANSIBLE / "k3s-storage-smoke.yml"
    assert own.exists(), (
        f"{own} is missing. The smoke play needs its own playbook; k3s-bringup.yml's "
        f"always-tagged server-host assert fires before any play appended there is reached."
    )
    bringup = (ANSIBLE / "k3s-bringup.yml").read_text()
    assert "storage_smoke" not in bringup, (
        "the storage smoke play is back in k3s-bringup.yml. Its first play asserts the "
        "invoking host is in k3s_server_hosts under `tags: always`, so this play is "
        "unreachable from daniel-server — the only host that can route to daniel-stage. "
        "Measured 2026-08-27: ok=3 failed=1, dying at the assert. Keep it in "
        "ansible/k3s-storage-smoke.yml."
    )
