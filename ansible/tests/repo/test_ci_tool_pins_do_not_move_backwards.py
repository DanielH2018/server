"""The tooling pins in ci.yml move forwards, and every copy of one moves together.

Two ways a Renovate bump to a CI tool pin silently no-ops, neither of which any existing check
reads (#1527, #1629):

- **It writes a version OLDER than the one on master.** `renovate/prek` carried
  `prek==0.5.0` into a `ci.yml` already on `0.5.2` (PR #1513). CI was green — the pin installs
  whatever it names — so the PR read as a bump while it was a downgrade. Renovate rebases a
  stale branch rather than recomputing it, so a branch cut before an earlier bump landed keeps
  naming the older version.
- **It rewrites one copy of a version and leaves another.** `pip install prek==` appears in TWO
  jobs, and the Vale download URL carries its version TWICE (the release tag and the asset
  name). A partial rewrite leaves one job installing the old tool, which is green and wrong.
  `renovate.json`'s Vale manager already carries two `matchStrings` for exactly this reason;
  nothing asserted the outcome.

The escape hatch is an inline `# downgrade-ok:` comment on the pin's own line. A version ratchet
with no override is a one-way door, and a yanked release makes a deliberate downgrade correct —
the marker goes where the decision is made rather than in a list somewhere else.

The comparison against `origin/master` needs that ref. It skips, naming the reason, when the ref
is not fetched — `test_ci_pytest_job_fetch_depth.py` is what keeps it resolvable on the runner,
the same arrangement `test_module_length_ratchet.py` relies on. A stale local ref compares
against older numbers, which can only make the check lenient.

Run: uv run pytest ansible/tests/repo/test_ci_tool_pins_do_not_move_backwards.py
"""

import re
import subprocess

import pytest

from _helpers import REPO

CI_REL = ".github/workflows/ci.yml"
CI_WORKFLOW = REPO / CI_REL

# Each pin, as the patterns that find EVERY copy of its version in ci.yml. A pin whose copies
# disagree is the partial-rewrite defect; the census below asserts these names are all found, so
# a renamed step or a dropped pin fails here rather than passing over nothing.
PINS: dict[str, tuple[str, ...]] = {
    "prek": (r"pip install prek==([\d.]+)",),
    "vale-cli/vale": (
        r"vale-cli/vale/releases/download/v([\d.]+)/",
        r"vale_([\d.]+)_Linux",
    ),
}

# Every pin this file knows about must be found. A pattern that stops matching returns an empty
# set, and `all()` over nothing passes — the repo's own vacuity failure class.
MUST_FIND = frozenset(PINS)

DOWNGRADE_MARKER = "downgrade-ok:"


# --- the rules, as predicates -------------------------------------------------------


def version_tuple(version: str) -> tuple[int, ...] | None:
    """`(0, 5, 2)` for a dotted-integer version, `None` for anything else.

    None means incomparable rather than equal: a pin that stopped being dotted integers must
    fail the census below, not quietly satisfy the ordering.
    """
    parts = version.split(".")
    if not all(part.isdigit() for part in parts) or not parts:
        return None
    return tuple(int(part) for part in parts)


def pin_moved_backwards(old: str, new: str) -> bool:
    """True when `new` names an older version than `old`. Unparseable versions are not backwards."""
    old_parsed, new_parsed = version_tuple(old), version_tuple(new)
    if old_parsed is None or new_parsed is None:
        return False
    return new_parsed < old_parsed


def copies_agree(versions: set[str]) -> bool:
    """True when every copy of one pin's version in the file reads the same.

    An empty set is NOT agreement — a pattern that matched nothing is the vacuity this guard
    exists to avoid, and the census asserts against it separately.
    """
    return len(versions) == 1


def versions_in(text: str, patterns: tuple[str, ...]) -> set[str]:
    return {match for pattern in patterns for match in re.findall(pattern, text)}


def downgrade_is_declared(text: str, patterns: tuple[str, ...]) -> bool:
    """True when the line carrying this pin also carries the `# downgrade-ok:` marker."""
    return any(
        DOWNGRADE_MARKER in line
        for line in text.splitlines()
        for pattern in patterns
        if re.search(pattern, line)
    )


def test_a_forward_bump_is_clean():
    assert not pin_moved_backwards("0.5.0", "0.5.2")


def test_an_unchanged_pin_is_clean():
    assert not pin_moved_backwards("0.5.2", "0.5.2")


def test_a_backwards_bump_is_flagged():
    """PR #1513 exactly: the branch wrote 0.5.0 into a ci.yml already on 0.5.2."""
    assert pin_moved_backwards("0.5.2", "0.5.0")


def test_a_backwards_bump_across_a_minor_is_flagged():
    assert pin_moved_backwards("3.20.0", "3.9.0")


def test_an_unparseable_version_is_not_called_backwards():
    assert not pin_moved_backwards("0.5.2", "0.6.0rc1")
    assert version_tuple("0.6.0rc1") is None


def test_agreeing_copies_are_clean():
    assert copies_agree({"0.5.2"})


def test_disagreeing_copies_are_flagged():
    """One job bumped, the other left behind — green, and one job runs the old tool."""
    assert not copies_agree({"0.5.2", "0.5.0"})


def test_no_copies_at_all_is_flagged():
    assert not copies_agree(set())


def test_a_declared_downgrade_is_recognised():
    assert downgrade_is_declared(
        "        run: pip install prek==0.5.0  # downgrade-ok: 0.5.2 was yanked",
        PINS["prek"],
    )


def test_an_undeclared_downgrade_is_not_recognised():
    assert not downgrade_is_declared(
        "        run: pip install prek==0.5.0  # pinned for a reproducible toolchain",
        PINS["prek"],
    )


# --- applied to the tree ------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def master_is_fetched() -> bool:
    return _git("rev-parse", "--verify", "origin/master").returncode == 0


@pytest.fixture(scope="module")
def ci_text() -> str:
    return CI_WORKFLOW.read_text()


def test_every_pin_this_guard_knows_about_is_present(ci_text):
    """Non-vacuity: a renamed install step would otherwise make every check below pass."""
    found = {name for name, patterns in PINS.items() if versions_in(ci_text, patterns)}
    assert found == MUST_FIND, (
        f"{CI_REL} no longer carries every pin this guard reads; missing "
        f"{sorted(MUST_FIND - found)}. Update PINS together with the workflow, or the checks "
        f"below pass over nothing"
    )


@pytest.mark.parametrize("pin", sorted(PINS))
def test_every_copy_of_a_pin_names_the_same_version(pin, ci_text):
    versions = versions_in(ci_text, PINS[pin])
    assert copies_agree(versions), (
        f"{pin} is pinned to more than one version in {CI_REL}: {sorted(versions)}. A partial "
        f"rewrite leaves one job installing the old tool, and CI stays green"
    )
    assert version_tuple(next(iter(versions))) is not None, (
        f"{pin} is pinned to {next(iter(versions))!r}, which is not dotted integers, so the "
        f"ordering check below cannot read it. Teach version_tuple the new shape"
    )


@pytest.mark.parametrize("pin", sorted(PINS))
def test_no_pin_moves_backwards_against_master(pin, ci_text):
    if not master_is_fetched():
        pytest.skip(
            "origin/master is not fetched, so there is nothing to compare against"
        )
    shown = _git("show", f"origin/master:{CI_REL}")
    assert shown.returncode == 0, (
        f"git show origin/master:{CI_REL} failed: {shown.stderr.strip()}"
    )
    master_versions = versions_in(shown.stdout, PINS[pin])
    if not master_versions:
        pytest.skip(
            f"{pin} is not pinned on master yet, so it cannot have moved backwards"
        )
    here = versions_in(ci_text, PINS[pin])
    if not copies_agree(here) or not copies_agree(master_versions):
        pytest.skip(
            f"{pin}'s copies disagree, which the agreement check above reports; comparing an "
            f"ambiguous version against master would report the same defect twice"
        )
    old, new = next(iter(master_versions)), next(iter(here))
    if not pin_moved_backwards(old, new):
        return
    assert downgrade_is_declared(ci_text, PINS[pin]), (
        f"{pin} goes from {old} on master to {new} here, which installs an OLDER tool while "
        f"reading as a bump (#1513). If the downgrade is deliberate — a yanked release — say so "
        f"with a `# {DOWNGRADE_MARKER} <reason>` comment on the pin's own line"
    )
