"""Why each red test failed on the unchanged code: an assertion, or a name that is not there yet.

WHY (#4023). The red gate accepts any `FAILED`. `red_prompt` tells the red author to import a
not-yet-existing module inside the test body, so `from new import f; assert f() is not None`
fails at the gate on the import and passes after any implementation of `f`. That test proves
the symbol was missing, not that its assertion can fail. The gate records which nodes failed
that way and refuses nothing: a refusal discards the whole red phase, and no production run
exists yet to calibrate one against. The reviewer is told which nodes to check for vacuity.

The failure text is the part of pytest's short summary after ` - `. It is cut to the terminal
width unless the run is `-vv`, which is how the red gate runs it.
"""

import re

_FAILED = re.compile(r"^FAILED (\S+) - (.*)$", re.M)
# A test that fails because the code under test is missing, before any assertion runs.
ABSENCE = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")
# A test that fails on what the code does.
ASSERTION = ("assert ", "AssertionError", "Failed: DID NOT RAISE")


def cause(text: str) -> str:
    """`absence`, `assertion` or `other` for one node's failure text."""
    if text.startswith(tuple(f"{name}:" for name in ABSENCE)):
        return "absence"
    if text.startswith(ASSERTION):
        return "assertion"
    return "other"


def red_by_absence(output: str, nodes: list[str]) -> list[str]:
    """The `nodes` that failed on a missing name in a `pytest -vv -rA` run's summary."""
    texts = {m.group(1): m.group(2) for m in _FAILED.finditer(output)}
    return [n for n in nodes if cause(texts.get(n, "")) == "absence"]
