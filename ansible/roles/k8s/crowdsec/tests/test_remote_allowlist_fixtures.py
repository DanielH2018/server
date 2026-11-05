"""The addresses `test_remote_allowlist.py` may hold. Failures name line numbers, never values.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_remote_allowlist_fixtures.py
"""

import ast
import ipaddress
import json
import re
from pathlib import Path

import pytest
from lib.proc_testing import run

import remote_allowlist as ra

TESTS_DIR = Path(__file__).parent
FIXTURE_FILE = "test_remote_allowlist.py"
ADDRESS_TOKEN = re.compile(r"[0-9A-Fa-f:.]+(?:/\d+)?")
EMBEDDED_IPV4 = re.compile(r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
FSTRING_IPV4_PREFIX = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){2})\.\{")
EMBEDDED_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,}")

PUBLIC_RESOLVERS = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "1.0.0.1",
        "1.1.1.1",
        "8.8.4.4",
        "8.8.8.8",
        "9.9.9.9",
        "149.112.112.112",
    )
)
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "2001:db8::/32",
    )
)
# The flag-out cases need a LAN and a CGNAT address, and neither is globally routable.
REFUSED_CLASS_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "100.64.0.0/10")
)


def _fixture_text():
    tracked = run(
        ["git", "ls-files", FIXTURE_FILE], cwd=TESTS_DIR, check=True
    ).stdout.split()
    assert tracked == [FIXTURE_FILE]
    return (TESTS_DIR / FIXTURE_FILE).read_text()


def _addresses_by_line(text):
    """Return (line number, address) for every IP address or CIDR literal in `text`."""
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for token in ADDRESS_TOKEN.findall(line):
            try:
                found.append((lineno, ipaddress.ip_interface(token).ip))
            except ValueError:
                continue
    return found


def _is_allowed(address):
    return address in PUBLIC_RESOLVERS or any(
        address in network
        for network in DOCUMENTATION_NETWORKS + REFUSED_CLASS_NETWORKS
    )


def _disallowed_lines(text):
    return sorted(
        {
            lineno
            for lineno, address in _addresses_by_line(text)
            if not _is_allowed(address)
        }
    )


def test_every_address_is_a_documentation_address_a_public_resolver_or_a_refused_class():
    assert _disallowed_lines(
        "a = '5.6.7.8'\nb = '203.0.113.1, 9.9.9.9, 10.0.0.1'\n"
    ) == [1]

    text = _fixture_text()
    found = {address for _, address in _addresses_by_line(text)}
    assert {ipaddress.ip_address("9.9.9.9"), ipaddress.ip_address("10.0.0.50")} <= found
    assert _disallowed_lines(text) == []


def test_every_sighting_the_allowlist_job_accepts_is_a_public_resolver():
    accepted = []
    for node in ast.walk(ast.parse(_fixture_text())):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_line"
        ):
            continue
        sighting = node.args[2]
        if not isinstance(sighting, ast.Constant):
            continue
        line = json.dumps(
            {
                "RouterName": "router",
                "DownstreamStatus": 200,
                "ClientHost": sighting.value,
            }
        )
        for host in ra.authenticated_clients([line], {"router"}):
            accepted.append((node.lineno, ipaddress.ip_address(host)))

    assert accepted, (
        "no fixture sighting is global, so the accepting cases prove nothing"
    )
    assert [lineno for lineno, host in accepted if host not in PUBLIC_RESOLVERS] == []


def _embedded_addresses(text):
    """Return every address in `text`, including those `ADDRESS_TOKEN` runs into punctuation."""
    found = [
        ipaddress.ip_address(match)
        for match in EMBEDDED_IPV4.findall(text)
        if _octets_fit(match)
    ]
    found += [
        ipaddress.ip_address(f"{prefix}.0")
        for prefix in FSTRING_IPV4_PREFIX.findall(text)
        if _octets_fit(f"{prefix}.0")
    ]
    for token in EMBEDDED_IPV6.findall(text):
        # A single trailing colon is sentence punctuation; `::` belongs to the address.
        candidate = (
            token[:-1] if token.endswith(":") and not token.endswith("::") else token
        )
        try:
            found.append(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return found


def _octets_fit(address):
    return all(int(octet) <= 255 for octet in address.split("."))


@pytest.mark.parametrize(
    "line",
    [
        '"5.6.7.8:443"',
        "# seen from 5.6.7.8.",
        "# exit 5.6.7.8: banned",
        '"abc5.6.7.8"',
        'f"5.6.7.{i}"',
        "# exit 2600:1234::1: banned",
        "# seen from 2600:1234::1.",
    ],
)
def test_an_address_inside_punctuation_is_flagged(line):
    assert not all(_is_allowed(address) for address in _embedded_addresses(line))


@pytest.mark.parametrize(
    "line",
    [
        '"9.9.9.9:443"',
        "# seen from 203.0.113.1.",
        'f"203.0.113.{i + 1}"',
        "# exit 2001:db8::1: banned",
        "argv[1:4], 12:00:00",
    ],
)
def test_a_permitted_address_inside_punctuation_is_clean(line):
    assert all(_is_allowed(address) for address in _embedded_addresses(line))


def test_no_fixture_line_hides_a_disallowed_address_in_punctuation():
    found = _embedded_addresses(_fixture_text())
    assert {
        ipaddress.ip_address("9.9.9.9"),
        ipaddress.ip_address("203.0.113.0"),
        ipaddress.ip_address("2001:db8:1:2:aaaa::1"),
    } <= set(found)
    assert [address for address in found if not _is_allowed(address)] == [], (
        "a fixture address is outside the permitted ranges"
    )
