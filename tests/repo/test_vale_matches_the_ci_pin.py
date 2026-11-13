"""The Vale binary the hosts install is the version CI installs.

The prek `vale` hook is `language = "system"`: it runs whatever binary sits on PATH. CI installs
one itself (`.github/workflows/ci.yml`), so a host without it fails the hook at commit time with
exit 127 while CI stays green. `hosts/packages/homelab-tools/package.yml` pins the binary the
`vale` component puts on PATH, which makes the version a fact stored in two places.

Two places is the failure class `test_ci_tool_pins_do_not_move_backwards.py` records: a Renovate bump that rewrites one copy leaves
the other behind, and both a green CI and a green local hook are consistent with the copies running
DIFFERENT rule sets, because a style package's rules change between releases. renovate.json's
`{{depName}}` group puts every manager's Vale bump in one PR; this guard is what fails when they
disagree anyway.

Sibling to `test_ci_tool_pins_do_not_move_backwards.py`, which owns the copies WITHIN ci.yml and
the never-move-backwards rule. This module owns the copies outside it.

Run: uv run pytest tests/repo/test_vale_matches_the_ci_pin.py
"""

import re

import pytest

from _helpers import REPO
from lib import yaml_fast

CI_REL = ".github/workflows/ci.yml"
TOOLS_REL = "hosts/packages/homelab-tools/package.yml"

# The CI patterns are `test_ci_tool_pins_do_not_move_backwards.py`'s, deliberately: this guard
# reads the same two copies that one does, so a rename there that this file did not follow shows
# up as a missing version here rather than as agreement over nothing.
CI_PATTERNS = (
    r"vale-cli/vale/releases/download/v([\d.]+)/",
    r"vale_([\d.]+)_Linux",
)


def versions_in(text: str, patterns: tuple[str, ...]) -> set[str]:
    return {match for pattern in patterns for match in re.findall(pattern, text)}


def tool_versions() -> set[str]:
    """Each architecture's pin of the `vale` tool, without its `v`."""
    tools = yaml_fast.safe_load((REPO / TOOLS_REL).read_text())["tools"]
    return {pin["version"].removeprefix("v") for pin in tools["vale"].values()}


def pins_agree(ci: set[str], host: set[str]) -> bool:
    """True when both files name exactly one version and it is the same one.

    An empty set on either side is NOT agreement. A pattern that stopped matching returns
    nothing, and a comparison over nothing would pass while checking nothing.
    """
    return len(ci) == 1 and ci == host


@pytest.mark.parametrize(
    ("ci", "host", "agree"),
    [
        pytest.param({"3.20.0"}, {"3.20.0"}, True, id="matching-pins-are-clean"),
        # The bump Renovate wrote into ci.yml alone: CI lints with 3.21.0, the host with 3.20.0.
        pytest.param({"3.21.0"}, {"3.20.0"}, False, id="host-left-on-the-old-version"),
        # The tag bumped, the asset name left behind — ci.yml names two versions, not one.
        pytest.param(
            {"3.21.0", "3.20.0"}, {"3.21.0"}, False, id="partially-rewritten-ci-url"
        ),
        pytest.param(set(), set(), False, id="neither-pattern-matches"),
        pytest.param({"3.20.0"}, set(), False, id="host-pattern-matches-nothing"),
    ],
)
def test_pins_agree_only_on_one_shared_version(
    ci: set[str], host: set[str], agree: bool
) -> None:
    assert pins_agree(ci, host) is agree


def test_both_files_still_carry_a_vale_pin():
    """Non-vacuity: a renamed step or a renamed var would otherwise pass over nothing."""
    ci = versions_in((REPO / CI_REL).read_text(), CI_PATTERNS)
    assert ci, f"{CI_REL} no longer carries a Vale version this guard can read"
    assert tool_versions(), f"{TOOLS_REL} no longer pins a Vale version"


def test_the_homelab_tools_pin_is_the_version_ci_installs():
    ci = versions_in((REPO / CI_REL).read_text(), CI_PATTERNS)
    assert pins_agree(ci, tool_versions()), (
        f"Vale is pinned to {sorted(ci)} in {CI_REL} and {sorted(tool_versions())} in "
        f"{TOOLS_REL}. Bump both in one PR, with each architecture's digest"
    )
