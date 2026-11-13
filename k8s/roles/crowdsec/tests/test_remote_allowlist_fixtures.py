"""The addresses `test_remote_allowlist.py` may hold. Failures name line numbers, never values.

Every assert compares line numbers or a boolean, and the scanner drops a malformed token
without its text, so a failure cannot print an address.

Run: uv run pytest k8s/roles/crowdsec/tests/test_remote_allowlist_fixtures.py
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
# Permitted addresses the fixture file holds in each shape the scanner handles. A scan that
# misses one of them has stopped reading that shape.
REQUIRED_MEMBERS = frozenset(
    ipaddress.ip_address(address)
    for address in ("9.9.9.9", "10.0.0.50", "203.0.113.0", "2001:db8:1:2:aaaa::1")
)


def _fixture_text():
    tracked = run(
        ["git", "ls-files", FIXTURE_FILE], cwd=TESTS_DIR, check=True
    ).stdout.split()
    assert tracked == [FIXTURE_FILE]
    return (TESTS_DIR / FIXTURE_FILE).read_text()


def _parse(token):
    """Return the address `token` spells, or None. The error text carries the token."""
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        return None


def _addresses(text):
    """Return (line number, address) for every address in `text`, punctuation or not."""
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        tokens = EMBEDDED_IPV4.findall(line)
        tokens += [f"{prefix}.0" for prefix in FSTRING_IPV4_PREFIX.findall(line)]
        for token in EMBEDDED_IPV6.findall(line):
            # A single trailing colon is sentence punctuation; `::` belongs to the address.
            ends_in_one_colon = token.endswith(":") and not token.endswith("::")
            tokens.append(token[:-1] if ends_in_one_colon else token)
        found += [(lineno, a) for a in map(_parse, tokens) if a is not None]
    return found


def _is_allowed(address):
    return address in PUBLIC_RESOLVERS or any(
        address in network
        for network in DOCUMENTATION_NETWORKS + REFUSED_CLASS_NETWORKS
    )


def _disallowed_lines(text):
    return sorted({n for n, address in _addresses(text) if not _is_allowed(address)})


def test_every_address_is_a_documentation_address_a_public_resolver_or_a_refused_class():
    planted = _disallowed_lines("a = '5.6.7.8'\nb = '203.0.113.1, 9.9.9.9, 10.0.0.1'\n")
    assert planted == [1]

    text = _fixture_text()
    found = {address for _, address in _addresses(text)}
    finds_every_required_member = REQUIRED_MEMBERS <= found
    assert finds_every_required_member
    lines = _disallowed_lines(text)
    assert lines == []


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
            accepted.append((node.lineno, _parse(host)))

    assert accepted, (
        "no fixture sighting is accepted, so the accepting cases prove nothing"
    )
    lines = [lineno for lineno, host in accepted if host not in PUBLIC_RESOLVERS]
    assert lines == []


# The ids name each shape: a planted address outside the permitted set must not reach a test id.
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
    ids=["port", "period", "colon", "letters", "fstring", "v6-colon", "v6-period"],
)
def test_an_address_inside_punctuation_is_flagged(line):
    flagged = bool(_disallowed_lines(line))
    assert flagged


@pytest.mark.parametrize(
    "line",
    [
        '"9.9.9.9:443"',
        "# seen from 203.0.113.1.",
        'f"203.0.113.{i + 1}"',
        "# exit 2001:db8::1: banned",
        "argv[1:4], 12:00:00",
    ],
    ids=["port", "period", "fstring", "v6-colon", "not-an-address"],
)
def test_a_permitted_address_inside_punctuation_is_clean(line):
    lines = _disallowed_lines(line)
    assert lines == []
