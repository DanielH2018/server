"""The Vale binary the hosts install is the version CI installs.

The prek `vale` hook is `language = "system"`: it runs whatever binary sits on PATH. CI installs
one itself (`.github/workflows/ci.yml`), so a host without it fails the hook at commit time with
exit 127 while CI stays green — issue #1703, worked around during PR #1702 by fetching the
tarball by hand into `~/.local/bin`. `roles/setup/initial_setup/tasks/host-basics.yml` now
installs it, which makes the version a fact stored in two places.

Two places is the `#1527`/`#1629` failure class: a Renovate bump that rewrites one copy leaves
the other behind, and both a green CI and a green local hook are consistent with the two running
DIFFERENT rule sets, because a style package's rules change between releases. renovate.json's
Vale manager names both files so one PR carries both; this guard is what fails when they
disagree anyway.

Sibling to `test_ci_tool_pins_do_not_move_backwards.py`, which owns the copies WITHIN ci.yml and
the never-move-backwards rule. This module owns the one copy outside it.

Run: uv run pytest ansible/tests/repo/test_vale_matches_the_ci_pin.py
"""

import re

from _helpers import REPO

CI_REL = ".github/workflows/ci.yml"
HOST_REL = "ansible/roles/setup/initial_setup/tasks/host-basics.yml"

# The CI patterns are `test_ci_tool_pins_do_not_move_backwards.py`'s, deliberately: this guard
# reads the same two copies that one does, so a rename there that this file did not follow shows
# up as a missing version here rather than as agreement over nothing.
CI_PATTERNS = (
    r"vale-cli/vale/releases/download/v([\d.]+)/",
    r"vale_([\d.]+)_Linux",
)
HOST_PATTERN = r'initial_setup_vale_version:\s*"([\d.]+)"'


def versions_in(text: str, patterns: tuple[str, ...]) -> set[str]:
    return {match for pattern in patterns for match in re.findall(pattern, text)}


def pins_agree(ci: set[str], host: set[str]) -> bool:
    """True when both files name exactly one version and it is the same one.

    An empty set on either side is NOT agreement. A pattern that stopped matching returns
    nothing, and a comparison over nothing would pass while checking nothing.
    """
    return len(ci) == 1 and ci == host


def test_matching_pins_are_clean():
    assert pins_agree({"3.20.0"}, {"3.20.0"})


def test_a_host_left_on_the_old_version_is_flagged():
    """The bump Renovate wrote into ci.yml alone: CI lints with 3.21.0, the host with 3.20.0."""
    assert not pins_agree({"3.21.0"}, {"3.20.0"})


def test_a_partially_rewritten_ci_url_is_flagged():
    """The tag bumped, the asset name left behind — ci.yml names two versions, not one."""
    assert not pins_agree({"3.21.0", "3.20.0"}, {"3.21.0"})


def test_a_pattern_that_matches_nothing_is_flagged():
    assert not pins_agree(set(), set())
    assert not pins_agree({"3.20.0"}, set())


def test_both_files_still_carry_a_vale_pin():
    """Non-vacuity: a renamed step or a renamed var would otherwise pass over nothing."""
    ci = versions_in((REPO / CI_REL).read_text(), CI_PATTERNS)
    host = versions_in((REPO / HOST_REL).read_text(), (HOST_PATTERN,))
    assert ci, f"{CI_REL} no longer carries a Vale version this guard can read"
    assert host, (
        f"{HOST_REL} no longer sets initial_setup_vale_version — either the install moved or "
        f"the var was renamed. Follow it here and in renovate.json's Vale manager"
    )


def test_the_host_installs_the_version_ci_installs():
    ci = versions_in((REPO / CI_REL).read_text(), CI_PATTERNS)
    host = versions_in((REPO / HOST_REL).read_text(), (HOST_PATTERN,))
    assert pins_agree(ci, host), (
        f"Vale is pinned to {sorted(ci)} in {CI_REL} and {sorted(host)} in {HOST_REL}. The hook "
        f'is `language = "system"`, so CI and a workstation would lint with different rules. '
        f"Bump both in one PR"
    )
