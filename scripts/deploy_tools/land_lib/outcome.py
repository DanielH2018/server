"""The words a landing ends with: the verdict set, the Outcome that carries one, and say().

An Outcome is raised by `Landing.die` and `Landing.finish` and returned by `pipeline.run`.
It carries verdict and exit code together, so neither can be printed without the other --
land.sh assigned LAND_VERDICT on the line before each `exit`, and a test grepped that they
were adjacent.
"""

from __future__ import annotations

import sys

VERDICTS = frozenset(
    {
        "settled",
        "unhealthy",
        "deploy-failed",
        "nothing-to-deploy",
        "blocked",
        "needs-manual-apply",
        "deferred",
        "merge-conflict",
        "pr-ci-red",
        "merge-timeout",
        "ci-red",
        "ci-timeout",
        "lock-busy",
    }
)


class Outcome(Exception):
    """How a landing ends: the exit code, the verdict, and what to print.

    An exit-75 outcome must name a verdict: before 2026-09-02 four of them reached the
    Landings board as one `aborted` bucket and were taken for lock contention.
    """

    def __init__(
        self, rc: int, detail: str, verdict: str | None = None, error: str | None = None
    ) -> None:
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        if rc == 75 and verdict is None:
            raise ValueError("an exit-75 outcome must name a verdict")
        super().__init__(detail)
        self.rc = rc
        self.detail = detail
        self.verdict = verdict
        self.error = error

    def emit(self) -> None:
        """Print `land: <error>` (stderr) and `VERDICT: <verdict> (<detail>)` (stdout), as present."""
        if self.error:
            print(f"land: {self.error}", file=sys.stderr)
        if self.verdict:
            print(f"VERDICT: {self.verdict} ({self.detail})")


def say(text: str) -> None:
    """A two-space-indented progress line, the shape the skill quotes."""
    print(f"  {text}")
