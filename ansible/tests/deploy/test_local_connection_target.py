"""The preamble refuses a `local`-connection host's play run from a different machine.

Both cluster nodes are pinned `ansible_connection=local` in inventory/hosts.ini, so
`-e target=<host>` picks that host's VARIABLES while every task executes on the machine the
command was typed on. Selection and execution are separate, and `target` controls only the
first. Ansible then prints a green recap for a play that touched the wrong host.

Measured 2026-08-29: `initial_setup.yml --tags setup_drift -e target=daniel-server`, run from
daniel-box, installed the setup-drift reader onto daniel-box and exited 0 while daniel-server
had none of it. The same invocation would put nut_host's UPS shutdown chain — gated to
`ups_host: daniel-server` — on the host with no UPS attached.

These tests EVALUATE the assert's own `that:` expression, pulled out of the YAML, against
fixture host state. A textual guard would pass against a rewritten expression that no longer
decides anything; this one re-runs whatever text is in the file. Each case is an accept/reject
pair, since a condition that fires on everything and one that fires on nothing are
indistinguishable from the passing side alone.
"""

import jinja2
import pytest
import yaml

from _helpers import ANSIBLE

PREAMBLE = ANSIBLE / "pre_tasks" / "load_secrets.yml"
INVENTORY = ANSIBLE / "inventory" / "hosts.ini"

CONTROLLER = "daniel-box"
LOCAL_PEER = "daniel-server"
SSH_HOST = "daniel-pi"


def _guard_expression():
    """The `that:` text of the wrong-machine assert, as written in the preamble."""
    tasks = yaml.safe_load(PREAMBLE.read_text())
    matches = [
        t
        for t in tasks
        if "assert" in t.get("name", "").lower() or "ansible.builtin.assert" in t
        if "local-connection" in t.get("name", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one wrong-machine assert in {PREAMBLE}, found {len(matches)}. "
        f"If it was renamed, update this test rather than deleting it."
    )
    that = matches[0]["ansible.builtin.assert"]["that"]
    # `that:` accepts a string or a list of strings; normalise to one boolean expression.
    return " and ".join(that) if isinstance(that, list) else that


def _evaluate(expression, *, inventory_hostname, connection, running_on):
    """Render the guard as Jinja2 with `lookup('pipe', 'hostname')` stubbed to `running_on`.

    Ansible evaluates `that:` as a Jinja expression, so this is the same decision the play
    makes — not a re-implementation of it.
    """
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["lookup"] = lambda kind, arg: (
        running_on
        if (kind, arg) == ("pipe", "hostname")
        else pytest.fail(
            f"the guard used an unexpected lookup({kind!r}, {arg!r}); this stub only knows "
            f"the hostname pipe, so teach it the new one deliberately."
        )
    )
    context = {"inventory_hostname": inventory_hostname}
    if connection is not None:
        context["ansible_connection"] = connection
    return env.compile_expression(expression)(**context)


def test_a_local_host_targeted_from_another_machine_is_refused():
    """The measured failure: daniel-box running the play for daniel-server."""
    assert not _evaluate(
        _guard_expression(),
        inventory_hostname=LOCAL_PEER,
        connection="local",
        running_on=CONTROLLER,
    ), (
        "the guard passes a play that selects a local-connection peer while executing on this "
        "machine — the 2026-08-29 misdeploy, which exits 0 and touches the wrong host."
    )


def test_a_local_host_targeted_on_its_own_machine_is_allowed():
    """The accept half: the same play run where it is meant to run."""
    assert _evaluate(
        _guard_expression(),
        inventory_hostname=LOCAL_PEER,
        connection="local",
        running_on=LOCAL_PEER,
    ), (
        "the guard blocks the correct invocation — running a local host's play on that host."
    )


def test_the_controllers_own_play_is_allowed():
    assert _evaluate(
        _guard_expression(),
        inventory_hostname=CONTROLLER,
        connection="local",
        running_on=CONTROLLER,
    ), "the guard blocks daniel-box deploying to itself, which is the ordinary case."


def test_an_ssh_host_is_never_blocked():
    """daniel-pi is reached over ssh, so where it executes is not in question."""
    assert _evaluate(
        _guard_expression(),
        inventory_hostname=SSH_HOST,
        connection="ssh",
        running_on=CONTROLLER,
    ), (
        "the guard blocks a genuine remote target; only `local` hosts can misdirect this way."
    )


def test_a_play_that_overrides_the_connection_to_ssh_is_allowed():
    """k3s-bringup.yml's agent-join play sets `ansible_connection: ssh` in its `vars:`.

    That is the repo's existing way of reaching daniel-server from daniel-box, and it must keep
    working — a guard that blocked it would push people toward disabling the guard.
    """
    assert _evaluate(
        _guard_expression(),
        inventory_hostname=LOCAL_PEER,
        connection="ssh",
        running_on=CONTROLLER,
    ), (
        "the guard blocks a play that deliberately overrode the connection to ssh, which is how "
        "k3s-bringup.yml joins the agent node."
    )


def test_an_unset_connection_does_not_trip_the_guard():
    """Ansible's default is ssh, so an inventory entry that names no connection cannot misdirect.

    The reject half of the `| default('ssh')`: without it, StrictUndefined raises instead of
    deciding, which would fail every play on a host that omits the setting.
    """
    assert _evaluate(
        _guard_expression(),
        inventory_hostname=SSH_HOST,
        connection=None,
        running_on=CONTROLLER,
    ), (
        "the guard errors or refuses when `ansible_connection` is unset; it must default to ssh."
    )


def test_the_inventory_still_pins_both_cluster_nodes_local():
    """The premise. If this stops being true the guard is inert, and inert reads as coverage.

    It is not an argument for changing the inventory — `local` is correct for a node running
    its own plays. It is what makes the guard necessary.
    """
    inventory = INVENTORY.read_text()
    for host in (CONTROLLER, LOCAL_PEER):
        assert f"{host}  ansible_connection=local" in inventory.replace("\t", " "), (
            f"{host} is no longer pinned `ansible_connection=local` in {INVENTORY}. If the "
            f"inventory changed deliberately, re-derive whether this guard still has a job."
        )


def test_the_guard_runs_per_host_and_not_run_once():
    """`run_once` would evaluate this for one host and apply the answer to the play.

    The sibling secrets assert is deliberately `run_once` because it asks a question about the
    play as a whole. This one asks about each host, so sharing one host's answer would let a
    misdirected host through behind a correctly-targeted one.
    """
    tasks = yaml.safe_load(PREAMBLE.read_text())
    guard = next(t for t in tasks if "local-connection" in t.get("name", ""))
    assert not guard.get("run_once"), (
        "the wrong-machine assert became run_once; it must evaluate per host, or a mixed play "
        "passes on the strength of whichever host Ansible reached first."
    )
    assert guard.get("tags") == "always" or "always" in (guard.get("tags") or []), (
        "the wrong-machine assert is not always-tagged, so a --tags run skips it — and a "
        "tag-scoped run is exactly how the 2026-08-29 misdeploy was invoked."
    )
