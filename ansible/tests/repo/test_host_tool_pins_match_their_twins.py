"""The uv tools the hosts install are pinned, and each pin equals its twin elsewhere in the tree.

`roles/setup/initial_setup/tasks/host-basics.yml` installs ansible-core, ansible-lint and prek
as uv tools. Until #2148 the install carried no version, so the ansible that ran every deploy
was whatever PyPI served on provisioning day: on 2026-09-21 daniel-server ran 2.21.0 and
daniel-box 2.21.2 against a 2.21.4 uv.lock. Each tool now names an exact version, and each
version is already chosen somewhere else:

- ansible-core: pyproject.toml's dev group (`ansible-core==`), the env the tests and
  `uv run ansible-playbook` resolve;
- ansible-lint: prek.toml's `ansible/ansible-lint` rev, the hook's own version;
- prek: ci.yml's `pip install prek==`, the runner's copy.

Two copies is the failure class `test_vale_matches_the_ci_pin.py` records: a Renovate bump that
rewrites one copy leaves the other behind, and both a green CI and a green host are consistent
with the two running different versions. renovate.json groups each pair into one PR; this guard
fails when they disagree anyway. Sibling to that Vale test, one module per twin class.

Run: uv run pytest ansible/tests/repo/test_host_tool_pins_match_their_twins.py
"""

import re

import pytest

from _helpers import REPO

HOST_REL = "ansible/roles/setup/initial_setup/tasks/host-basics.yml"

# (tool, twin file, twin pattern). The host pattern is the loop item `- <tool>==<version>`.
TWINS = (
    ("ansible-core", "pyproject.toml", r'"ansible-core==([\d.]+)"'),
    ("ansible-lint", "prek.toml", r'ansible/ansible-lint"\s+rev\s*=\s*"v([\d.]+)"'),
    ("prek", ".github/workflows/ci.yml", r"pip install prek==([\d.]+)"),
)


def host_pattern(tool: str) -> str:
    return rf"^\s*- {re.escape(tool)}==([\d.]+)\s*$"


def versions_in(text: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, text, flags=re.MULTILINE))


def pins_agree(twin: set[str], host: set[str]) -> bool:
    """True when both sides name exactly one version and it is the same one.

    An empty set on either side is NOT agreement: a pattern that stopped matching returns
    nothing, and a comparison over nothing would pass while checking nothing.
    """
    return len(twin) == 1 and twin == host


def test_matching_pins_are_clean():
    assert pins_agree({"2.21.4"}, {"2.21.4"})


def test_a_host_left_behind_its_twin_is_flagged():
    """The bump Renovate wrote into pyproject.toml alone."""
    assert not pins_agree({"2.21.5"}, {"2.21.4"})


def test_a_pattern_that_matches_nothing_is_flagged():
    assert not pins_agree(set(), set())
    assert not pins_agree({"2.21.4"}, set())


def test_an_unpinned_host_tool_is_flagged():
    """The pre-#2148 line, `- ansible-core`, reads as no version at all."""
    assert (
        versions_in("  loop:\n    - ansible-core\n", host_pattern("ansible-core"))
        == set()
    )
    assert versions_in(
        "    - ansible-core==2.21.4\n", host_pattern("ansible-core")
    ) == {"2.21.4"}


@pytest.mark.parametrize(
    ("tool", "twin_rel", "twin_pattern"), TWINS, ids=[t[0] for t in TWINS]
)
def test_the_host_installs_the_version_its_twin_names(tool, twin_rel, twin_pattern):
    twin = versions_in((REPO / twin_rel).read_text(), twin_pattern)
    host = versions_in((REPO / HOST_REL).read_text(), host_pattern(tool))
    assert twin, f"{twin_rel} no longer carries a {tool} pin this guard can read"
    assert host, (
        f"{HOST_REL} no longer installs `{tool}==<version>` -- either the loop item lost its "
        f"pin or the tool moved. Follow it here and in renovate.json"
    )
    assert pins_agree(twin, host), (
        f"{tool} is {sorted(twin)} in {twin_rel} and {sorted(host)} in {HOST_REL}. "
        f"Bump both in one PR"
    )
