"""The `kubeconfig` tag survives a `--check` run, which needs one opt-out per retried read.

WHY THIS EXISTS. Check mode SKIPS `ansible.builtin.command` — Ansible cannot simulate an
arbitrary binary, so it returns `skipped: true` with no `stdout`. A task that also retries
then evaluates its until-condition against that skip result, which never satisfies it, so the
task burns every retry and FAILS the play. The dry run reports a red that says nothing about
the tree.

Measured 2026-09-22 (#2293): `ansible-playbook ansible/k3s-bringup.yml --tags kubeconfig
--check --diff` failed at `Read the read-only ServiceAccount token` after 12 attempts and a
minute of `delay: 5`, while the real run of the same tag passed with the token read
succeeding first try. `.claude/rules/ansible.md` asks for a `--check` before touching
production state, so a tag that always fails under `--check` trains an operator to skip the
check or to ignore what it says.

SCOPE IS DELIBERATELY ONE TASK FILE, and the measurement is why. The tree holds 21 retried
command tasks; 19 of them are waits for state an EARLIER task in the same play creates —
`Wait for the build to finish`, `Wait for the PVC to reach Bound`, `Wait for the
re-encryption to finish`. Check mode skips that producer too, so making those run for real
would have them wait out their retries on something that is never going to appear: the
opt-out is wrong for them, not merely unapplied. `changed_when: false` does not sort the two
apart either — 19 of the 21 carry it, the waits included. Telling a read of pre-existing
state from a wait on this play's own output needs a human reading the task, so a tree-wide
guard would be a demand for 19 changes that should not be made. The rest of the class is
issue #2305.

Run: uv run pytest ansible/tests/setup/test_k3s_kubeconfig_dry_run.py
"""

import pytest
from lib import yaml_fast

from _helpers import ROLES, walk_tasks

KUBECONFIG_TASKS = ROLES / "setup" / "k3s" / "tasks" / "kubeconfig.yml"

# The modules check mode cannot simulate, so it skips them outright. `uri`, `stat`, `slurp`
# and the file modules are deliberately absent: they implement check mode and return a real
# result, so a retry loop over one of them already works under `--check`.
SKIPPED_IN_CHECK_MODE = frozenset(
    {
        "command",
        "shell",
        "raw",
        "script",
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "ansible.builtin.raw",
        "ansible.builtin.script",
    }
)

# The retried tasks this file must still contain, so a rename or a move cannot empty the
# census and leave the assertion below passing over nothing.
KNOWN_RETRIED_READS = frozenset({"Read the read-only ServiceAccount token"})


def check_mode_retry_problem(task: dict) -> str | None:
    """Why `task` fails its own retry loop under `--check`, or None when it is fine.

    A reason rather than a bool, so the failure names the defect instead of the file.
    """
    if not SKIPPED_IN_CHECK_MODE & set(task):
        return None
    if "retries" not in task and "until" not in task:
        return None
    if task.get("check_mode") is not False:
        return (
            "retries under a module check mode skips, without `check_mode: false` — a "
            "`--check` run burns every retry on a skip result and fails the play"
        )
    if task.get("changed_when") is not False:
        return (
            "`check_mode: false` without `changed_when: false` — the task opts out of the "
            "dry run without declaring itself a read, so `--check` would change state"
        )
    return None


def _retried_tasks() -> list[dict]:
    tasks = yaml_fast.safe_load(KUBECONFIG_TASKS.read_text()) or []
    return [
        t
        for t in walk_tasks(tasks)
        if SKIPPED_IN_CHECK_MODE & set(t) and ("retries" in t or "until" in t)
    ]


def test_the_census_still_finds_the_retried_reads_it_knows_about():
    names = {str(t.get("name", "")) for t in _retried_tasks()}
    missing = KNOWN_RETRIED_READS - names
    assert not missing, (
        f"kubeconfig.yml no longer has a retried task named {sorted(missing)} — it was "
        "renamed or removed; update KNOWN_RETRIED_READS in the same commit, or this file "
        "checks an empty set and passes"
    )


def test_every_retried_read_in_the_kubeconfig_tag_runs_under_check_mode():
    offenders = [
        f"{t.get('name', '<unnamed>')} — {why}"
        for t in _retried_tasks()
        if (why := check_mode_retry_problem(t))
    ]
    assert not offenders, "`--tags kubeconfig --check` fails on:\n" + "\n".join(
        offenders
    )


# ── the rule itself, both directions ──────────────────────────────────────────────────────
# A rule that fires on everything and one that fires on nothing are indistinguishable from
# the passing side alone, so each branch has an accepting and a rejecting input.


def _task(**kw):
    return {"name": "t", "ansible.builtin.command": {"cmd": "true"}, **kw}


@pytest.mark.parametrize(
    "task",
    [
        _task(retries=12, until="r.stdout | length > 0"),
        _task(until="r.stdout | length > 0"),
        _task(retries=3, check_mode=True),
    ],
)
def test_a_retried_command_without_the_opt_out_is_flagged(task):
    assert check_mode_retry_problem(task) is not None


def test_a_read_that_opts_out_of_check_mode_is_clean():
    task = _task(retries=12, until="r.stdout", check_mode=False, changed_when=False)
    assert check_mode_retry_problem(task) is None


def test_a_state_changing_task_may_not_opt_out_of_check_mode():
    task = _task(retries=12, until="r.rc == 0", check_mode=False)
    assert "changed_when" in (check_mode_retry_problem(task) or "")


@pytest.mark.parametrize(
    "task",
    [
        {
            "name": "t",
            "ansible.builtin.uri": {"url": "x"},
            "retries": 5,
            "until": "r.status == 200",
        },
        _task(),
    ],
)
def test_a_task_the_rule_does_not_govern_is_clean(task):
    assert check_mode_retry_problem(task) is None
