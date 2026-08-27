"""`k3s-bringup.yml` installs a k3s server only on a host named in `k3s_server_hosts`.

The play used to assert `inventory_hostname == 'daniel-box'`. Staging adds a second cluster
whose single node is also a server, so the assert had to admit more than one name — and the
direction it was widened in is the whole point.

An allowlist refuses a host nobody thought about. A denylist (`not in k3s_agent_hosts`)
admits one, which is the same shape as the accident the assert exists to prevent: standing
up a second control plane on daniel-server, a node that already belongs to prod's cluster as
an agent. k3s ships its own containerd and its own iptables rules, so the damage is to a
live node rather than to a spare one.

The textual half matters as much as the value half. Folding the condition into a derived
fact would read as a tidy-up and would hide the guard from the test that enforces it — the
`textual-guard-checks-break-on-indirection` failure — so this file reads the assert's own
expression and requires it to name the variable.
"""

import re

import pytest
import yaml

from _helpers import ANSIBLE

BRINGUP = ANSIBLE / "k3s-bringup.yml"
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"
HOSTS_INI = ANSIBLE / "inventory" / "hosts.ini"

# The agent node. Named rather than derived: this is the specific host the assert exists to
# keep out, and deriving it from the same inventory the test checks would be circular.
AGENT_HOST = "daniel-server"


def _server_hosts() -> list[str]:
    return yaml.safe_load(ALL_VARS.read_text())["k3s_server_hosts"]


def _inventory_hosts() -> set[str]:
    """Hostnames declared in hosts.ini, ignoring group headers and per-host settings."""
    out = set()
    for line in HOSTS_INI.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            out.add(line.split()[0])
    return out


def _bringup_assert_expr() -> str:
    """The `that:` expression of the play's server-host guard."""
    play = yaml.safe_load(BRINGUP.read_text())[0]
    for task in play.get("pre_tasks", []):
        block = task.get("ansible.builtin.assert") or task.get("assert")
        if block and "k3s_server_hosts" in str(block.get("that", "")):
            return str(block["that"])
    pytest.fail(
        f"no assert naming k3s_server_hosts found in the first play of {BRINGUP}. The "
        f"server-host guard is what stops a second control plane landing on {AGENT_HOST}."
    )


def test_the_guard_is_an_allowlist_naming_the_variable():
    expr = _bringup_assert_expr()
    assert re.fullmatch(r"inventory_hostname in k3s_server_hosts", expr.strip()), (
        f"the server-host guard in {BRINGUP} reads {expr!r}. Keep it the literal "
        f"`inventory_hostname in k3s_server_hosts`: a negated or derived form admits every "
        f"host nobody remembered to deny, and an indirection hides the guard from this test."
    )


def test_the_agent_node_is_not_a_server_host():
    """The specific accident: a second control plane on a node already in prod's cluster."""
    assert AGENT_HOST not in _server_hosts(), (
        f"{AGENT_HOST} is listed in k3s_server_hosts ({ALL_VARS}). It is an AGENT in prod's "
        f"cluster — installing a k3s server there gives one node two control planes. To "
        f"rejoin it as an agent use k3s-bringup.yml -e join_agent={AGENT_HOST}."
    )


def test_every_server_host_exists_in_the_inventory():
    unknown = set(_server_hosts()) - _inventory_hosts()
    assert not unknown, (
        f"k3s_server_hosts names {sorted(unknown)}, which {HOSTS_INI} does not declare. The "
        f"assert would refuse every run against those names while reading as permissive."
    )


def test_the_prod_control_plane_is_still_a_server_host():
    """Guards the derivation: an empty or truncated list would make the tests above vacuous."""
    hosts = _server_hosts()
    assert "daniel-box" in hosts, (
        f"daniel-box is missing from k3s_server_hosts ({ALL_VARS}), so k3s-bringup.yml "
        f"refuses to run against prod's own control plane."
    )
    assert len(hosts) == len(set(hosts)), f"k3s_server_hosts has duplicates: {hosts}"
