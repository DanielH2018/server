"""Every setup-plane task that reads the repo checkout ON THE TARGET is gated.

`daniel-stage` is the one host Ansible drives over ssh that holds no checkout of this repo, so
`{{ playbook_dir }}` and `~/server/...` name paths that exist on the controller and not on the
target. `docs/staging-cluster.md` audited this by hand and found two such tasks; this census
finds five, and the two it added are ones nobody would have hit until the setup plane was first
pointed at staging.

WHICH SIDE READS THE PATH IS THE WHOLE QUESTION. `template.src` and `copy.src` are read by
Ansible on the CONTROLLER and shipped as content, so naming the checkout there is fine whatever
the target is — the same reasoning `staging-cluster.md` already applies to `lookup()`. But
`command.argv`, `cron.job`, `args.chdir` and `copy.dest` resolve ON THE TARGET, and those are
the ones that break. Classifying by module argument rather than by grepping the task is what
separates them, and grepping is what made the hand audit come up short.

THE REJECTING HALF MATTERS MORE THAN USUAL. Of the five, two fail loudly and three install a
cron SUCCESSFULLY against a path that does not exist — the deploy reads green and the failure
lands at 06:00 on a Sunday, on stderr nothing reads.
"""

import pytest
import yaml
from lib import yaml_fast

from _helpers import ALL_VARS, HOST_VARS, SETUP_ROLES

GATE = "has_repo_checkout"

# A path that only resolves where this repo is checked out.
NEEDLES = ("}}/server", "playbook_dir", "~/server")

# Module arguments Ansible resolves on the CONTROLLER. Naming the checkout in one of these says
# nothing about the target, so it is not a hit.
CONTROLLER_SIDE = frozenset({"src", "content", "fail_msg", "msg", "that"})

# The census must find at least these. Named rather than counted: a rename or a move must fail
# with the member that went missing, and an assertion over an empty census passes vacuously.
KNOWN_HITS = frozenset(
    {
        "Install Git hooks",
        "Create cron job to clear ansible.log weekly",
        "Install pinned Ansible collections from requirements.yml",
        "Seed ansible/.sops.yaml (first-host bootstrap only)",
        "Schedule the etcd restore drill",
    }
)

# Tasks this census once found and that were then FIXED rather than gated. Listed so a fix
# that regresses — the pruner reaching back into `{{ playbook_dir }}` — fails as a named
# member, not as one more entry in the gated list. `release_bin.yml` ships the pruner inside
# the release and runs it through `current` (issue #923); the release-commit read moved to the
# controller with `delegate_to: localhost` (PR #919).
KNOWN_FIXED = frozenset(
    {
        "Prune superseded releases for {{ release_bin_group }}",
        "Read the deploy commit for host scripts",
    }
)


def _clauses(when: object) -> list:
    if when is None:
        return []
    return list(when) if isinstance(when, list) else [when]


def _flatten(tasks: object, inherited: list | None = None) -> list[dict]:
    """Every task, with `block`/`rescue`/`always` children lifted out of their container.

    A child inherits its block's `when`, because Ansible applies it to each child rather than to
    the block as a unit. Dropping it here would report a task the block already gates.
    """
    inherited = inherited or []
    out: list[dict] = []
    if not isinstance(tasks, list):
        return out
    for task in tasks:
        if not isinstance(task, dict):
            continue
        combined = inherited + _clauses(task.get("when"))
        nested = False
        for key in ("block", "rescue", "always"):
            if key in task:
                nested = True
                out += _flatten(task[key], combined)
        if not nested:
            out.append({**task, "when": combined or None})
    return out


def _target_side_hits(task: dict) -> list[str]:
    """Module arguments in `task` that name the checkout and are resolved on the target."""
    # A delegated task runs on the controller, so every path in it is the controller's.
    if str(task.get("delegate_to", "")) in ("localhost", "127.0.0.1"):
        return []
    hits = []
    for key, value in task.items():
        if key in ("name", "when", "tags", "register", "vars"):
            continue
        args = value if isinstance(value, dict) else {key: value}
        for arg, val in args.items():
            text = str(val)
            if not any(needle in text for needle in NEEDLES):
                continue
            if arg in CONTROLLER_SIDE:
                continue
            # A lookup() runs on the controller whatever the target's connection type.
            if "lookup(" in text:
                continue
            hits.append(f"{key}.{arg}" if isinstance(value, dict) else key)
    return hits


def _census() -> list[tuple[str, str, object, list[str]]]:
    """(file, task name, its `when`, the offending arguments) for every target-side hit."""
    found = []
    for tasks_file in sorted(SETUP_ROLES.glob("*/tasks/*.yml")):
        try:
            parsed = yaml_fast.safe_load(tasks_file.read_text())
        except yaml.YAMLError:
            continue
        for task in _flatten(parsed):
            hits = _target_side_hits(task)
            if hits:
                found.append(
                    (
                        f"{tasks_file.parent.parent.name}/{tasks_file.name}",
                        task.get("name", "?"),
                        task.get("when"),
                        hits,
                    )
                )
    return found


def _gated(when: object) -> bool:
    """Whether this `when` keeps the task off a host with no checkout.

    A host pin counts too: `inventory_hostname == 'daniel-box'` says strictly more than the gate,
    since daniel-box holds a checkout by construction.
    """
    if when is None:
        return False
    clauses = when if isinstance(when, list) else [when]
    text = " ".join(str(clause) for clause in clauses)
    return GATE in text or "inventory_hostname ==" in text


def test_the_census_finds_the_tasks_it_is_meant_to_check():
    """Non-vacuity. This census globs for its own subject, so a rename or a move would empty it
    and leave every assertion below passing over nothing."""
    names = {name for _f, name, _w, _h in _census()}
    missing = KNOWN_HITS - names
    assert not missing, f"census no longer finds: {sorted(missing)}"


def test_a_fixed_task_stays_off_the_checkout():
    """A task that was fixed rather than gated must not reappear in the census at all. Gating
    it again would pass the test below and quietly re-open the hole the fix closed."""
    names = {name for _f, name, _w, _h in _census()}
    regressed = KNOWN_FIXED & names
    assert not regressed, f"reads the checkout on the target again: {sorted(regressed)}"


def test_the_fixed_tasks_still_exist():
    """Non-vacuity for the test above: a renamed task would pass it by vanishing."""
    names = set()
    for tasks_file in SETUP_ROLES.glob("*/tasks/*.yml"):
        try:
            parsed = yaml_fast.safe_load(tasks_file.read_text())
        except yaml.YAMLError:
            continue
        names |= {t.get("name") for t in _flatten(parsed)}
    missing = KNOWN_FIXED - names
    assert not missing, f"no task named: {sorted(missing)}"


@pytest.mark.parametrize("entry", _census(), ids=lambda e: f"{e[0]}::{e[1]}")
def test_every_target_side_checkout_read_is_gated(entry):
    tasks_file, name, when, hits = entry
    assert _gated(when), (
        f"{tasks_file}: {name!r} reads the repo checkout on the TARGET via {hits}, "
        f"but its `when` is {when!r}. daniel-stage has no checkout. Add `{GATE}`."
    )


def test_a_controller_side_read_is_not_a_hit():
    """`template.src` is read on the controller and shipped as content, so it needs no gate.
    Without this the census would demand a gate on every role that ships a file."""
    assert not _target_side_hits(
        {
            "ansible.builtin.template": {
                "src": "{{ playbook_dir }}/../x.j2",
                "dest": "/etc/x",
            }
        }
    )


def test_a_target_side_read_is_a_hit():
    """The accepting half of the pair above."""
    assert _target_side_hits(
        {"ansible.builtin.cron": {"job": "/home/{{ sys_user }}/server/x.sh"}}
    )


def test_a_delegated_task_is_not_a_hit():
    """`delegate_to: localhost` moves the whole task to the controller, checkout and all."""
    assert not _target_side_hits(
        {
            "delegate_to": "localhost",
            "ansible.builtin.command": {"chdir": "{{ playbook_dir }}/.."},
        }
    )


def test_the_same_task_undelegated_is_a_hit():
    """The rejecting half: it is the delegation doing the work, not the module."""
    assert _target_side_hits(
        {"ansible.builtin.command": {"chdir": "{{ playbook_dir }}/.."}}
    )


def test_an_ungated_task_is_rejected():
    """Without this, a `_gated` that returned True unconditionally would look identical to a
    clean plane."""
    assert not _gated(None)
    assert not _gated("ansible_os_family == 'Debian'")
    assert not _gated(["has_docker", "has_gitops"])


def test_the_gate_is_accepted_in_both_when_forms():
    assert _gated(GATE)
    assert _gated([GATE, "not something_else"])


def test_daniel_stage_is_the_host_that_declares_no_checkout():
    """The gate is worthless if no host sets it false."""
    stage = yaml_fast.safe_load((HOST_VARS / "daniel-stage.yml").read_text())
    assert stage[GATE] is False
    defaults = yaml_fast.safe_load(ALL_VARS.read_text())
    assert defaults[GATE] is True, (
        "the default must stay true, or every host skips these"
    )


def test_daniel_pi_keeps_its_checkout_despite_being_a_remote_host():
    """Verified against the live host on 2026-09-02: /home/ubuntu/server/ansible exists on the
    Pi. Deriving this gate from `ansible_connection` would have skipped tasks that work there."""
    pi = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())
    assert pi.get(GATE, True) is True
