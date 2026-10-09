"""Guard: the host forwarder's metrics listener must bind the wildcard, not a node address.

WHY. A `prometheus <addr>:<port>` line naming the node's own routable address can only bind
once that address is assigned. If it is not, CoreDNS comes up, serves DNS on
`bind 127.0.0.1`, binds no metrics listener, and systemd reports the unit healthy with
`NRestarts=0`. A wildcard bind depends on no address
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

from _helpers import load_yaml
from lib.repo_paths import K3S_DEFAULTS
from _setup_render import rendered_setup_text

_COREFILE = "host-corefile.j2"
_PORT_VAR = "k3s_host_dns_metrics_port"
# The operand runs to the end of the line rather than to the first space, because a bracketed
# IPv6 wildcard (`[::]:9253`) carries no space either.
_PROMETHEUS = re.compile(r"^[ \t]*prometheus[ \t]+(.+?)[ \t]*$", re.M)


def expected_port() -> int:
    """The port the role's defaults publish, which the render must agree with.

    Read from defaults rather than written as a literal: the assertion below is that the
    template INTERPOLATES the variable, and against a literal expectation a template that
    hardcoded the same number would pass.
    """
    return load_yaml(K3S_DEFAULTS)[_PORT_VAR]


def metrics_bind_problem(corefile: str, port: int) -> str | None:
    """The failure message when the metrics listener is not on the wildcard, else None.

    Takes a RENDERED Corefile and the port it should carry. Reading the template's source
    instead would check that the operand spells `{{ k3s_host_dns_metrics_port }}`, which says
    nothing about what the host ends up listening on and matches nothing once the expression
    is spelled any other way (#3202).
    """
    directives = _PROMETHEUS.findall(corefile)
    if not directives:
        return "no `prometheus` directive: the host forwarder exports no metrics at all"
    if len(directives) > 1:
        return f"{len(directives)} `prometheus` directives; expected exactly one"
    address, _, rendered_port = directives[0].rpartition(":")
    if rendered_port != str(port):
        return (
            f"the metrics listener renders port {rendered_port!r} rather than {port}, the "
            f"value of {_PORT_VAR}; the scrape job interpolates that variable and would "
            "target a closed port"
        )
    if address not in ("0.0.0.0", "[::]"):
        return (
            f"the metrics listener binds {address!r} rather than the wildcard; a bind to a "
            "specific address fails when that address is assigned after CoreDNS starts, and "
            "the unit stays green with no listener (issue #1476)"
        )
    return None


def test_the_live_corefile_binds_the_wildcard() -> None:
    problem = metrics_bind_problem(
        rendered_setup_text("k3s", _COREFILE), expected_port()
    )
    assert problem is None, problem


def test_a_wildcard_bind_is_clean() -> None:
    assert metrics_bind_problem("    prometheus 0.0.0.0:9253\n", 9253) is None


def test_a_node_address_bind_is_flagged() -> None:
    """The shape that shipped: correct once the address is up, absent when it is not yet."""
    problem = metrics_bind_problem("    prometheus 10.0.0.5:9253\n", 9253)
    assert problem is not None
    assert "wildcard" in problem


def test_a_loopback_bind_is_flagged() -> None:
    """Binds reliably and is scraped by nobody: Prometheus scrapes from a pod."""
    problem = metrics_bind_problem("    prometheus 127.0.0.1:9253\n", 9253)
    assert problem is not None
    assert "127.0.0.1" in problem


def test_a_port_the_variable_does_not_hold_is_flagged() -> None:
    """A port hardcoded in the template reads as a working listener and is scraped by nobody."""
    problem = metrics_bind_problem("    prometheus 0.0.0.0:9253\n", 9254)
    assert problem is not None
    assert "closed port" in problem


def test_a_missing_directive_is_flagged() -> None:
    assert metrics_bind_problem("    cache 30\n", 9253) == (
        "no `prometheus` directive: the host forwarder exports no metrics at all"
    )
