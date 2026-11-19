"""The rsyslog filter drops the kernel's pod-to-pod martian lines and keeps every other martian.

The filter's `re_match` pattern is read out of the shipped file and run with Python's `re`,
which agrees with POSIX ERE on the subset it uses. The sample lines are the two kernel formats
seen in /var/log/syslog: daniel-box runs 6.8, daniel-server 7.0.
"""

import ipaddress
import re

import pytest
from _helpers import ALL_VARS
from _host_packages import shipped_text
from lib import yaml_fast

FILTER_DEST = "/etc/rsyslog.d/49-homelab-info-filter.conf"
FILTER_HOSTS = ("daniel-box", "daniel-server")

POD_MARTIANS = (
    "IPv4: martian source 10.42.0.17 from 10.42.0.9, on dev eth0",
    "IPv4: martian source (src=10.42.1.173, dst=10.42.1.178, dev=eth0)",
)
OTHER_MARTIANS = (
    "IPv4: martian source 10.0.0.1 from 10.0.0.89, on dev eno1",
    "IPv4: martian source (src=10.0.0.89, dst=10.0.0.1, dev=enp88s0)",
    "IPv4: martian source (src=10.42.1.5, dst=255.255.255.255, dev=wlo1)",
    "IPv4: martian destination 10.42.0.3 from 10.42.0.4, dev eno1",
)
LL_HEADER = "ll header: 00000000: ff ff ff ff ff ff 4a 9d 71 0e 73 a7 08 06"


def _drops(host: str, facility: str, msg: str) -> bool:
    """Whether the martian rule in `host`'s shipped filter discards this message."""
    text = shipped_text(host, FILTER_DEST)
    (literal,) = re.findall(r're_match\(\$msg, "((?:[^"\\]|\\.)*)"\)', text)
    pattern = literal.replace("\\\\", "\\")
    return facility == "kern" and (
        re.search(pattern, msg) is not None or "ll header: " in msg
    )


@pytest.mark.parametrize("host", FILTER_HOSTS)
@pytest.mark.parametrize("msg", [*POD_MARTIANS, LL_HEADER])
def test_a_pod_to_pod_martian_and_its_hex_line_are_flagged(host: str, msg: str):
    assert _drops(host, "kern", msg), msg


@pytest.mark.parametrize("host", FILTER_HOSTS)
@pytest.mark.parametrize("msg", OTHER_MARTIANS)
def test_a_martian_from_outside_the_pod_network_is_clean(host: str, msg: str):
    assert not _drops(host, "kern", msg), msg


def test_a_pod_martian_on_another_facility_is_clean():
    assert not _drops("daniel-server", "user", POD_MARTIANS[0])


def test_the_pattern_names_the_inventorys_pod_cidr():
    """The pattern spells out the /16's first two octets, so a moved pod CIDR has to move it too."""
    cidr = ipaddress.ip_network(
        yaml_fast.safe_load(ALL_VARS.read_text())["k3s_pod_cidr"]
    )
    assert cidr.prefixlen == 16, cidr
    first, second = str(cidr.network_address).split(".")[:2]
    for host in FILTER_HOSTS:
        text = shipped_text(host, FILTER_DEST)
        assert text.count(f"{first}\\\\.{second}\\\\.") == 4, text
