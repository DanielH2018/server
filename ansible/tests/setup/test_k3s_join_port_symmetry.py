#!/usr/bin/env python3
"""The two k3s join-port allowlists must stay symmetric apart from one named exception.

`k3s_join_server_ports` (allowed FROM each agent, ON the server) and `k3s_join_agent_ports`
(allowed FROM the server, ON the agent) describe the same cluster traffic from opposite ends.
Element-wise the only legitimate asymmetry is `6443/tcp`: agents dial the server's API, and the
server never dials theirs.

`defaults/main.yml:801` already states the invariant — "The mirror of the agent-side 9100 rule
below, and it must stay symmetric" — and until this file nothing enforced it. A grep across
`ansible/tests/` for either variable returned zero readers (2026-08-23b review M9).

The class has already fired. The agent-side 9100 rule was added 2026-08-14 on the assumption
that "prometheus runs on the server"; the 2026-08-23 07:38 reboot moved that single-replica,
un-pinned Deployment to daniel-server, and daniel-box's node target then sat down 5.4 hours,
dropping host memory and /boot coverage entirely. The server-side mirror closed it. An
asymmetric list is that outage, pre-staged, and nothing else in the repo would notice.

WHAT THIS DOES NOT CATCH, and it matters: a port missing from BOTH lists is symmetric by
construction. MetalLB's memberlist port 7946 sat in exactly that blind spot from the day the
agent joined until 2026-08-23b — absent from both lists, blocked on both nodes, gossip never
converging — and no symmetry test could have found it. It is in both lists now, which changes
nothing about the blind spot. This guards one shape of firewall defect, not firewall
completeness; only a live probe from the node that needs a port can establish that.

Run: uv run pytest ansible/tests/setup/test_k3s_join_port_symmetry.py
"""

from _helpers import SETUP_ROLES, load_yaml

K3S = SETUP_ROLES / "k3s"

# The one direction-specific port, with the reason it is legitimately one-sided. Anything else
# appearing on only one side is a bug, not a decision.
SERVER_ONLY = {("6443", "tcp")}


def _ports(variable: str) -> set[tuple[str, str]]:
    defaults = load_yaml(K3S / "defaults" / "main.yml")
    assert variable in defaults, f"{variable} is gone from k3s defaults/main.yml"
    entries = defaults[variable]
    assert entries, f"{variable} is empty — this guard would pass vacuously"
    return {(str(entry["port"]), str(entry["proto"])) for entry in entries}


def test_server_side_covers_every_agent_side_port():
    """Every port the server opens toward agents must be open on the agents toward the server.

    This is the direction the 2026-08-23 node-exporter outage broke: the agent side had 9100 and
    the server side did not, so whichever node Prometheus was NOT on could not be scraped.
    """
    server = _ports("k3s_join_server_ports")
    agent = _ports("k3s_join_agent_ports")
    missing = agent - server
    assert not missing, (
        f"k3s_join_agent_ports opens {sorted(missing)} that k3s_join_server_ports does not "
        f"mirror. Both ends carry the same cluster traffic; a one-sided rule means whichever "
        f"node the caller lands on decides whether it works."
    )


def test_agent_side_covers_every_server_side_port_but_the_named_exception():
    server = _ports("k3s_join_server_ports")
    agent = _ports("k3s_join_agent_ports")
    missing = server - agent - SERVER_ONLY
    assert not missing, (
        f"k3s_join_server_ports opens {sorted(missing)} that k3s_join_agent_ports does not "
        f"mirror, and it is not in the documented server-only set {sorted(SERVER_ONLY)}. Either "
        f"mirror it or add it to SERVER_ONLY here with the reason it is one-sided."
    )


def test_the_server_only_exception_is_still_real():
    """Guards the exemption itself:

    if 6443 leaves the server list, SERVER_ONLY is silently excusing a port nobody opens, and the
    test above weakens without failing.
    """
    server = _ports("k3s_join_server_ports")
    stale = SERVER_ONLY - server
    assert not stale, (
        f"SERVER_ONLY exempts {sorted(stale)}, which k3s_join_server_ports no longer opens. An "
        f"exemption for a rule that does not exist quietly widens what the symmetry check "
        f"tolerates."
    )
