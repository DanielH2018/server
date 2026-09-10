"""Guard: the host forwarder's metrics listener must bind the wildcard, not a node address.

WHY. A `prometheus <addr>:<port>` line naming the node's own routable address can only bind
once that address is assigned. On the 2026-09-09 boot of daniel-box it was not: CoreDNS came
up, served DNS on `bind 127.0.0.1`, bound no metrics listener, and systemd reported the unit
healthy with `NRestarts=0` all day (GitHub issue #1476). A wildcard bind depends on no address
being assigned first, so it cannot fail that way.

WHY IT NEEDS A GUARD. Nothing on the node reports the missing listener — not the unit, not the
journal, not `probe.py health`. The only signal is a Prometheus target several layers away
going down, and it names a connection refusal rather than a bind failure. Re-introducing the
node address is a one-word edit that renders, parses, and deploys green.

The exposure argument is settled elsewhere: UFW's INPUT is default-deny, and
`roles/setup/k3s/tasks/node.yml` opens this port to the pod CIDR and the agent nodes only.
`test_coredns_metrics_port_allows_agent_nodes.py` guards that half.
"""

import re

from _helpers import ROLES as _ROLES

_COREFILE = _ROLES / "setup/k3s/templates/host-corefile.j2"
_PORT_VAR = "k3s_host_dns_metrics_port"
# The operand runs to the end of the line rather than to the first space: it is a Jinja
# expression, `0.0.0.0:{{ k3s_host_dns_metrics_port }}`, and a `\S+` capture matches none of it.
_PROMETHEUS = re.compile(r"^[ \t]*prometheus[ \t]+(.+?)[ \t]*$", re.M)


def metrics_bind_problem(corefile: str) -> str | None:
    """The failure message when the metrics listener is not on the wildcard, else None."""
    directives = _PROMETHEUS.findall(corefile)
    if not directives:
        return "no `prometheus` directive: the host forwarder exports no metrics at all"
    if len(directives) > 1:
        return f"{len(directives)} `prometheus` directives; expected exactly one"
    address, _, port = directives[0].rpartition(":")
    if _PORT_VAR not in port:
        return (
            f"the metrics listener is on port {port!r} rather than {{{{ {_PORT_VAR} }}}}; "
            "the scrape job interpolates that variable and would target a closed port"
        )
    if address not in ("0.0.0.0", "[::]"):
        return (
            f"the metrics listener binds {address!r} rather than the wildcard; a bind to a "
            "specific address fails when that address is assigned after CoreDNS starts, and "
            "the unit stays green with no listener (issue #1476)"
        )
    return None


def test_the_live_corefile_binds_the_wildcard() -> None:
    problem = metrics_bind_problem(_COREFILE.read_text())
    assert problem is None, problem


def test_a_wildcard_bind_is_clean() -> None:
    assert (
        metrics_bind_problem("    prometheus 0.0.0.0:{{ k3s_host_dns_metrics_port }}\n")
        is None
    )


def test_a_node_address_bind_is_flagged() -> None:
    """The shape that shipped: correct once the address is up, absent when it is not yet."""
    problem = metrics_bind_problem(
        "    prometheus {{ server_ip }}:{{ k3s_host_dns_metrics_port }}\n"
    )
    assert problem is not None
    assert "wildcard" in problem


def test_a_loopback_bind_is_flagged() -> None:
    """Binds reliably and is scraped by nobody: Prometheus scrapes from a pod."""
    problem = metrics_bind_problem(
        "    prometheus 127.0.0.1:{{ k3s_host_dns_metrics_port }}\n"
    )
    assert problem is not None
    assert "127.0.0.1" in problem


def test_a_literal_port_is_flagged() -> None:
    problem = metrics_bind_problem("    prometheus 0.0.0.0:9253\n")
    assert problem is not None
    assert "closed port" in problem


def test_a_missing_directive_is_flagged() -> None:
    assert metrics_bind_problem("    cache 30\n") == (
        "no `prometheus` directive: the host forwarder exports no metrics at all"
    )
