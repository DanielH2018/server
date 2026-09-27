"""The host crons and monitor-bridge must probe the SAME endpoints, issue #2793.

The WAN gate has two halves: monitor-bridge's `wan_reachable` check, which suppresses the four
bridge checks that reach the internet, and `wan_reachable` in kuma-push-lib.sh, which the four host
crons consult. They are separate code because they run in separate places — a pod loop and a host
cron — and nothing but this guard stops them disagreeing about what "the internet" means.

The disagreement matters in one direction in particular. If the crons probed an endpoint the
bridge does not, an outage of that one endpoint alone would make every host tile report a skip
while the WAN tile stayed green, so the outage would be reported by nothing at all.
"""

import re

from lib import yaml_fast
from _helpers import ALL_VARS, ROLES

CONFIG_IO = ROLES / "k8s/monitor-bridge/files/bridge/config_io.py"


def _bridge_default_urls() -> list[str]:
    """`WAN_PROBE_DEFAULT` as the list `config_io` splits it into.

    Read as text rather than imported: this suite does not put the bridge's `files/` on sys.path,
    and the constant is a plain string literal.
    """
    text = CONFIG_IO.read_text()
    match = re.search(r'WAN_PROBE_DEFAULT = \(\s*"([^"]+)"\s*\)', text)
    assert match, "WAN_PROBE_DEFAULT is no longer a single parenthesised string literal"
    return [u.strip() for u in match.group(1).split(",") if u.strip()]


def test_the_host_cron_endpoint_list_equals_the_bridge_default():
    declared = yaml_fast.safe_load(ALL_VARS.read_text())["wan_probe_urls"]
    assert declared == _bridge_default_urls()


def test_the_list_names_two_independent_providers():
    # Non-vacuity, and the property the Python gate's docstring turns on: one provider's own
    # outage must not silence a caller reading the other, so a single-entry list is a defect
    # however well the equality above holds.
    declared = yaml_fast.safe_load(ALL_VARS.read_text())["wan_probe_urls"]
    hosts = {re.sub(r"^https?://([^/]+).*$", r"\1", u) for u in declared}
    assert len(hosts) >= 2, hosts
    # By hostname, never an anycast IP: the 2026-09-18 outage included DNS failure, and an
    # IP-only probe stays green through a DNS-only outage while every dependent fails.
    for host in hosts:
        assert not re.fullmatch(r"[\d.]+", host), f"{host} is an IP, not a hostname"
