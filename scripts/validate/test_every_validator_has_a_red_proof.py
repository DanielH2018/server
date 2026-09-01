#!/usr/bin/env python3
"""Every validator ships with a test that proves it can go RED.

CLAUDE.md requires this of any new validator, guard, health check or probe: one input it must
accept, and one it must reject. Until now the rule lived only in prose, and the repo has paid
for that twice — `volume-claim`'s short-circuit shipped behind 16 passing tests and a mutation
test, then fired for 0 of 25 claims across two full deploys; `image-smoke`'s bare-boot rule
never caught a real image problem across 11 failures. Both read green throughout. A check is
only ever observed passing, so without the rejecting half there is no evidence it can fail.

WHAT THIS ENFORCES, AND WHAT IT CANNOT. It looks for a red-proof SIGNAL in the test module's
source: an assertion that the validator reported a problem, or a `pytest.raises`. That is a
proxy for "this suite exercises the failing path", and a determined author can satisfy it
without meaning it. It is not a proxy for the naming convention — an earlier draft matched test
NAMES and would have failed `test_validate_config_templates.py`, whose red proof is really
there under the name `test_yaml_error_passes_valid_and_catches_invalid`. A guard that fires on
a correct suite is worse than no guard, because it gets switched off.

SCOPE IS DELIBERATELY NARROW. The five modules in `scripts/validate/`, derived by glob rather
than listed. 46 of 156 test files in this repo carry an explicit rejecting half, but most of the
rest are direct config assertions that need no red proof — they ARE the check. Policing all of
them would produce a guard nobody could land, which is the failure mode described above.

Run: uv run pytest scripts/validate/test_every_validator_has_a_red_proof.py
"""

import re
from pathlib import Path

import pytest

VALIDATE = Path(__file__).resolve().parent

# Signals that a suite asserts the validator REPORTS something, rather than only that a clean
# input stays clean. Body-level on purpose: the name is where the convention lives, but the
# assertion is where the evidence is.
RED_PROOF = re.compile(
    r"pytest\.raises"
    r"|assert\s+(problems|errors|failures|found|flagged|missing)\b"
    r"|catches_invalid"
    r"|_is_flagged|_is_detected|_is_rejected|_rejects\b"
    r"|assert\s+[^\n]*==\s*1\b"
)


def validators() -> list[Path]:
    return sorted(VALIDATE.glob("validate_*.py"))


def test_the_scan_finds_the_validators():
    """Without this, every parametrized test below passes vacuously on an empty glob."""
    assert len(validators()) >= 5


@pytest.mark.parametrize("validator", validators(), ids=lambda p: p.stem)
def test_every_validator_has_a_test_module(validator: Path):
    assert (VALIDATE / f"test_{validator.name}").is_file(), (
        f"{validator.name} has no test module. A validator with no test is a check nobody has "
        f"seen fail."
    )


@pytest.mark.parametrize("validator", validators(), ids=lambda p: p.stem)
def test_every_validator_has_a_proof_it_can_go_red(validator: Path):
    tests = VALIDATE / f"test_{validator.name}"
    if not tests.is_file():
        pytest.skip("covered by test_every_validator_has_a_test_module")
    assert RED_PROOF.search(tests.read_text(errors="replace")), (
        f"{tests.name} never asserts that {validator.name} reports a problem. Add the rejecting "
        f"half: an input the validator must flag, asserted on the problem list it returns. "
        f"test_validate_compose_templates.py is the worked example — every rule there is a "
        f"`..._is_clean` / `..._is_flagged` pair."
    )


def test_the_guard_rejects_a_suite_with_no_red_proof():
    """This guard is itself a check, so it ships with its own rejecting input.

    Without this, a RED_PROOF pattern that had silently stopped matching would leave every
    parametrized test above passing on the empty search it now performs.
    """
    accepting_only = (
        "def test_a_valid_template_is_clean():\n    assert validate(GOOD) == []\n"
    )
    assert not RED_PROOF.search(accepting_only)

    with_red_proof = accepting_only + (
        "def test_a_broken_template_is_flagged():\n"
        "    problems = validate(BAD)\n"
        "    assert problems\n"
    )
    assert RED_PROOF.search(with_red_proof)
