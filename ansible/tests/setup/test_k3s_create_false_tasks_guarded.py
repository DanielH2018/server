"""A `create: false` line/block edit in the k3s role must be guarded by a stat.

`blockinfile` and `lineinfile` with `create: false` do not skip a missing path — they FAIL:

    Path /etc/zsh/zshenv does not exist !

That is the right behaviour for a file the play expects to exist, and the wrong behaviour for
an optional one. The k3s role edits `/etc/zsh/zshenv` to make its kubeconfig hook reach zsh,
with `create: false` chosen deliberately: writing a zsh startup file onto a host with no zsh
leaves a trap for whoever installs zsh later.

Both prod nodes run zsh, so nothing exercised the missing-file path until the first
`k3s-bringup.yml` run against `daniel-stage` on 2026-08-27 — a stock Ubuntu cloud image with
no `/etc/zsh` at all. It failed the play after 75 tasks, with the cluster otherwise up.

Derived over the role's task files rather than naming that one task, so a sibling written the
same way is covered. The fix is always a guard, never flipping `create: true` — that would
create the file this decision exists to avoid.
"""

import pytest

from _helpers import ROLES, leaf_tasks, load_tasks

TASK_DIR = ROLES / "setup" / "k3s" / "tasks"
MODULES = (
    "ansible.builtin.blockinfile",
    "ansible.builtin.lineinfile",
    "blockinfile",
    "lineinfile",
)


def _create_false_tasks():
    """(task file, task name, task) for every create: false line/block edit in the role."""
    found = []
    for path in sorted(TASK_DIR.glob("*.yml")):
        for task in leaf_tasks(load_tasks(path)):
            for module in MODULES:
                args = task.get(module)
                if isinstance(args, dict) and args.get("create") is False:
                    found.append((path.name, task.get("name", "<unnamed>"), task))
    return found


def test_some_create_false_tasks_exist():
    """Guards the derivation: an empty list would make the check below vacuous."""
    assert _create_false_tasks(), (
        f"no `create: false` line/block edits found under {TASK_DIR}. Either they were "
        f"removed, or the module names this test matches on have changed."
    )


@pytest.mark.parametrize(
    "filename,name,task",
    _create_false_tasks(),
    ids=[f"{f}::{n}" for f, n, _ in _create_false_tasks()],
)
def test_a_create_false_task_is_guarded_by_a_when(filename, name, task):
    when = task.get("when")
    assert when, (
        f"{filename} task {name!r} edits a file with `create: false` and has no `when:`. "
        f"blockinfile/lineinfile FAIL on a missing path rather than skipping, so on a host "
        f"without that file this aborts the play. Guard it with a stat and "
        f"`when: <reg>.stat.exists` — do not set `create: true`, which would write the file "
        f"onto a host that should not have it."
    )
    assert "stat.exists" in str(when), (
        f"{filename} task {name!r} is guarded by {when!r}, which does not test whether the "
        f"file exists. `create: false` fails on a missing path, so the guard has to be the "
        f"existence check itself."
    )
