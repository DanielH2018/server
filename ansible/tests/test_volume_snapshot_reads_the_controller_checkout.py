"""A task reading the repo checkout must run on the CONTROLLER, not on the play's target.

k8s/volume-snapshot resolves the deploy tag with `git rev-parse --short=8 HEAD` and
`chdir: "{{ playbook_dir }}/.."`. That SHA is a property of the controller's checkout: it names
the commit whose templates the run is rendering, which is what lets slice 7b find a snapshot
from the deploy that created it.

Reading it on the target only ever coincided with that. `ansible/inventory/hosts.ini` pins both
daniel-box and daniel-server to `ansible_connection=local`, so "the target" and "the controller"
were the same machine and the same checkout, and the missing `delegate_to` was invisible.
daniel-stage is the first genuinely remote target in this repo. It has no checkout, and the task
fails there with "Unable to change directory before execution" — in a role with thirteen callers.

Nothing else catches this. The role's other tests stub `k3s kubectl` and run against the local
checkout, where the two hosts are the same thing; `--check` skips the command entirely; and a
rendered-expression test sees a `chdir` that is correct on its face. What is wrong is WHERE the
command runs, which is only visible in the task's own keywords.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "ansible" / "tests"))

_ROLE = _REPO / "ansible" / "roles" / "k8s" / "volume-snapshot"
_CONTROLLER = "localhost"


def _flatten(tasks) -> list[dict]:
    """Every task, including those nested in block/rescue/always."""
    out: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_flatten(task.get(key)))
    return out


def _chdir_of(task: dict) -> str | None:
    """The `chdir` a command/shell task runs in, from either spelling.

    Ansible accepts it inside the module args or under a sibling `args:` mapping, and this role
    uses the first. Reading both means a later move between the two spellings cannot silently
    drop the task out of this check's scope.
    """
    for key in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(key)
        if isinstance(spec, dict) and spec.get("chdir"):
            return str(spec["chdir"])
    args = task.get("args")
    if isinstance(args, dict) and args.get("chdir"):
        return str(args["chdir"])
    return None


def undelegated_checkout_reads(tasks) -> list[str]:
    """The verdict: names of tasks that read the repo checkout on the wrong host.

    A function rather than an inline comparison so the rejecting half below drives the SAME
    verdict the real check drives, instead of asserting arithmetic of its own.
    """
    found = []
    for task in _flatten(tasks):
        chdir = _chdir_of(task)
        if not chdir or "playbook_dir" not in chdir:
            continue
        if task.get("delegate_to") != _CONTROLLER:
            found.append(str(task.get("name") or "<unnamed task>"))
    return found


def _role_tasks(name: str):
    return yaml.safe_load((_ROLE / "tasks" / name).read_text())


@pytest.mark.parametrize(
    "tasks_file", sorted(p.name for p in (_ROLE / "tasks").glob("*.yml"))
)
def test_every_checkout_read_runs_on_the_controller(tasks_file: str) -> None:
    offenders = undelegated_checkout_reads(_role_tasks(tasks_file))
    assert not offenders, (
        f"{tasks_file}: {offenders} chdir into the repo checkout without "
        f"`delegate_to: {_CONTROLLER}`, so they read whichever checkout the TARGET has. On a "
        f"genuinely remote target there is none, and the task fails with 'Unable to change "
        f"directory before execution'."
    )


def test_the_check_rejects_the_shape_that_was_live() -> None:
    """The rejecting half, built as the exact task this role carried before the fix.

    Without it a check that stopped matching — a moved `chdir`, a renamed module key — would
    report clean forever, which is the failure mode this repo has paid for twice.
    """
    before_the_fix = [
        {
            "name": "Resolve the deploy tag",
            "ansible.builtin.command": {
                "argv": ["git", "rev-parse", "--short=8", "HEAD"],
                "chdir": "{{ playbook_dir }}/..",
            },
        }
    ]
    assert undelegated_checkout_reads(before_the_fix) == ["Resolve the deploy tag"], (
        "The pre-fix task is no longer reported, so this check cannot see the bug it exists "
        "for and its green says nothing."
    )


def test_the_check_accepts_the_delegated_form() -> None:
    """The accepting half. A check that flagged everything would pass the test above too."""
    fixed = [
        {
            "name": "Resolve the deploy tag",
            "delegate_to": _CONTROLLER,
            "ansible.builtin.command": {
                "argv": ["git", "rev-parse", "--short=8", "HEAD"],
                "chdir": "{{ playbook_dir }}/..",
            },
        }
    ]
    assert undelegated_checkout_reads(fixed) == []


def test_the_role_really_has_a_checkout_read_to_govern() -> None:
    """Pins the premise. If the `git rev-parse` ever moves out of this role, the parametrised
    check above keeps passing over a corpus that no longer contains the thing it governs."""
    reads = [
        task
        for name in (_ROLE / "tasks").glob("*.yml")
        for task in _flatten(_role_tasks(name.name))
        if (_chdir_of(task) or "").find("playbook_dir") >= 0
    ]
    assert reads, (
        "k8s/volume-snapshot no longer chdirs into the checkout anywhere, so this file governs "
        "nothing — move the check to wherever the deploy tag is resolved now, or retire it."
    )
