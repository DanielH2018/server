"""The cross-file timeout sums nothing else pins.

"The deployer's apply survives max flock contention" is split across defaults/main.yml (the
broad apply budget) and gitops-deploy.service.j2 (the flock wait and TimeoutStartSec). Four
other jobs wait on the same lock, so every one of their waits must clear the deployer's worst
hold. Every value is read from its source rather than pinned, so a bump to any one of them
fails here instead of silently reopening the gap.

Since D6.12 deleted the k8s path, the broad apply is the one phase a tick runs under the lock:
the forward k8s deploy and its rollback, the two terms these sums were first built for, are
gone with their budgets.

The lock waiters are checked as a CENSUS (`_LOCK_WAITERS`) rather than one test each. A
per-consumer test only ever covers the consumers somebody remembered to write one for.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py

import pathlib
import re

import pytest
import yaml

from lib.repo_paths import REPO as _REPO

from _shell_render import rendered_shell_text

# The phase budget is measured from the moment its phase starts, which is AFTER flock acquires,
# while TimeoutStartSec counts from unit activation and so INCLUDES the flock wait. The worst
# case is therefore flock_wait + the phase budget, and it must fit inside TimeoutStartSec, else
# systemd SIGTERMs the deployer mid-apply with no hold written. Nothing else pins the cross-file
# sum, so a later bump to any one value would silently reopen it while every other test stays
# green.

_TEMPLATES = pathlib.Path(__file__).parents[1] / "templates"


def _search1(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    assert m is not None, f"pattern {pattern!r} did not match — template renamed?"
    return m.group(1)


def _systemd_seconds(span: str) -> int:
    # Parse the systemd time spans this unit actually uses (Nmin / Ns / bare seconds).
    m = re.fullmatch(r"(\d+)\s*(min|m|sec|s|)", span.strip())
    assert m is not None, f"unrecognized systemd time span {span!r}"
    return int(m.group(1)) * (60 if m.group(2) in ("min", "m") else 1)


# The value is a Jinja reference in config.env.j2, not a literal, so this reads its source —
# defaults/main.yml — instead of the rendered template.
_DEFAULTS = pathlib.Path(__file__).parents[1] / "defaults" / "main.yml"


def _worst_lock_hold(defaults: dict) -> int:
    """The git-tree lock hold of one activation, EXCLUDING this unit's own flock wait.

    The broad arm is the one phase left: `BROAD_DEPLOY_TIMEOUT_S` bounds its whole apply loop
    (`deploy_handlers.handle_broad`), and the arm is forward-only, so no rollback budget
    stacks on top.

    The term bounds its phase WHOLE: since ADR-0017 an apply also waits for its service locks,
    and it waits while this unit holds the git-tree lock. `deploy_locks.locked_budget` is what
    keeps the term true — it shares one deadline between that wait and the playbook.
    `ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py::test_a_budgeted_deploy_shares_one_deadline_between_its_wait_and_its_run`
    is the guard that fails if the wait gets a budget of its own.
    """
    return int(defaults["gitops_deploy_broad_timeout_s"])


def _budget_fits(defaults: dict, flock_wait: int, timeout_start: int) -> bool:
    return flock_wait + _worst_lock_hold(defaults) <= timeout_start


def test_the_deployers_hold_survives_max_flock_contention():
    unit = (_TEMPLATES / "gitops-deploy.service.j2").read_text()
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    flock_wait = int(_search1(r"^ExecStart=.*?flock\s+-w\s+(\d+)", unit))
    timeout_start = _systemd_seconds(_search1(r"^TimeoutStartSec=(\S+)", unit))
    hold = _worst_lock_hold(defaults)
    assert _budget_fits(defaults, flock_wait, timeout_start), (
        f"flock -w {flock_wait} + the worst-case lock hold {hold}s "
        f"(BROAD_DEPLOY_TIMEOUT_S) = {flock_wait + hold}s must fit inside TimeoutStartSec "
        f"{timeout_start}s, or a stalled broad apply gets SIGTERMed with no hold written."
    )


def test_a_budget_that_outgrows_the_ceiling_is_caught():
    # Red proof for the budget tests that share _worst_lock_hold. They can only ever be
    # observed passing, so this drives the same verdict function with a shape that must fail.
    sized = {"gitops_deploy_broad_timeout_s": 1800}
    assert _worst_lock_hold(sized) == 1800
    assert _budget_fits(sized, 180, 2700)

    over = {"gitops_deploy_broad_timeout_s": 3600}
    assert not _budget_fits(over, 180, 2700), (
        "the budget check must REJECT a shape that overruns (180 + 3600 = 3780s against "
        "TimeoutStartSec 2700s); a check that passes it is measuring nothing."
    )


_SECRET_ROTATE = (
    _REPO / "hosts/components/secret-rotation/templates/secret-rotate.sh.j2"
)

# Every job that waits for the git-tree lock, with the regex that reads its wait.
#
# The three cron templates are read RENDERED: their wait lives in git-tree-lock.j2, the macro
# each one imports (#3722), so the template source no longer holds the number at all.
#
# WHY A CENSUS AND NOT ONE TEST EACH. A per-consumer test only covers the consumers somebody
# remembered to write one for. This table is the thing a new waiter has to be added to, and the
# non-vacuity test below is what makes forgetting fail rather than pass silently.
_LOCK_WAITERS = {
    "secret-rotate.sh.j2": (_SECRET_ROTATE, r"^flock\s+-w\s+(\d+)\s+9"),
    "docs-refresh.sh.j2": (
        _REPO / "hosts/components/docs-refresh/templates/docs-refresh.sh.j2",
        r"^flock\s+-w\s+(\d+)\s+9",
    ),
    "eval-run.sh.j2": (
        _REPO / "hosts/components/eval-sweep/templates/eval-run.sh.j2",
        r"^flock\s+-w\s+(\d+)\s+9",
    ),
    "deploy_lib/under_locks.py": (
        _REPO / "scripts" / "deploy_tools" / "deploy_lib" / "under_locks.py",
        r"^LOCK_WAIT = (\d+)$",
    ),
}


def test_the_lock_waiter_census_is_non_vacuous():
    # Guards _LOCK_WAITERS itself. A census that silently lost a member — a renamed template, a
    # bad edit — would leave the parametrized test below iterating over whatever survived and
    # still passing, which is the failure this whole table exists to stop. Assert the names, not
    # a count, so the message says WHICH waiter went missing.
    assert set(_LOCK_WAITERS) == {
        "secret-rotate.sh.j2",
        "docs-refresh.sh.j2",
        "eval-run.sh.j2",
        "deploy_lib/under_locks.py",
    }
    for name, (path, _) in _LOCK_WAITERS.items():
        assert path.is_file(), (
            f"{name}: {path} is gone; the census now checks nothing for it"
        )


def _waiter_text(path: pathlib.Path) -> str:
    # What ships: a template is read as its render, a Python module as its source.
    if path.suffix == ".j2" and path.parent.parent.parent.name == "components":
        return rendered_shell_text("components", path.parent.parent.name, path.name)
    if path.suffix == ".j2":
        return rendered_shell_text("setup", "initial_setup", path.name)
    return path.read_text()


@pytest.mark.parametrize("name", sorted(_LOCK_WAITERS))
def test_every_git_tree_lock_waiter_clears_the_deployers_worst_case_hold(name):
    # gitops-deploy.service wraps its whole ExecStart in the git-tree lock, and one
    # activation runs the broad apply budget inside that one hold. A waiter that gives up early does not fail safe: it fires its unit's OnFailure= alert for
    # ordinary contention AND skips that run. For the weekly secret-rotate that means the next
    # attempt is +7 days, and ROTATE_LEAD_DAYS=8 against a 7-day cadence means a token usually
    # gets exactly one eligible run, so a skipped week can put a token overdue.
    #
    # Derived from the same defaults the deployer reads, so bumping the budget fails this
    # instead of silently shortening every waiter at once.
    path, pattern = _LOCK_WAITERS[name]
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    worst_hold = _worst_lock_hold(defaults)

    wait = int(_search1(pattern, _waiter_text(path)))
    assert wait >= worst_hold, (
        f"{name}'s git-tree lock wait of {wait}s must clear gitops-deploy's worst-case lock "
        f"hold ({worst_hold}s: BROAD_DEPLOY_TIMEOUT_S), or a "
        f"legitimate long apply makes this job skip a run "
        f"and page for ordinary contention."
    )


def test_a_short_lock_waiter_is_flagged():
    # Red proof for the parametrized test above, which can only ever be observed passing. This
    # is a waiter at 1200 against the 1800s hold.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert 1200 < _worst_lock_hold(defaults), (
        "the worst-case hold must exceed 1200 for this red proof to mean anything; if the "
        "budget shrank below it, re-derive this number rather than deleting the test."
    )
