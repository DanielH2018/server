"""Verify-by: the prose an issue stores about how to check it, and how `verify` reports it.

`findings.py open --verify-by` stores a description of how to verify the finding in the issue
body; `findings.py verify` prints those descriptions back. Nothing here executes anything.

WHY NOT A COMMAND. Until 2026-09-06 a verify-by was a shell command, and `verify` ran it and
read its exit code as the verdict. Measured over the register on that date: of the 17 closed
findings that carried one, 3 cleared the read-only classifier guarding the run and 12 were
genuine read-only commands it refused, so the executable half mostly never executed. It also
required running text read out of a GitHub issue body, which is why the classifier and a
hand-written `uv run` allowlist stood in front of it at all. Prose cannot execute, so both the
gate and the thing it guarded are gone (#1313, #1351).

The rendering lives here rather than in `findings.py` so the report — including its counts —
is a pure function a test can call without a CLI or a gh read.
"""

import textwrap

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.issue_model import parse_verify_by

WRAP_WIDTH = 88


def verification_instructions(issue: dict) -> str | None:
    """The prose stored under `## Verify-by`, or None when the issue carries none."""
    return parse_verify_by(issue.get("body") or "")


def _wrap(instructions: str, indent: str) -> list[str]:
    """The instructions as indented display lines, one paragraph's blank line preserved."""
    lines: list[str] = []
    for paragraph in instructions.split("\n\n"):
        if lines:
            lines.append("")
        if not paragraph.strip():
            continue
        lines += textwrap.wrap(
            " ".join(paragraph.split()),
            width=WRAP_WIDTH,
            initial_indent=indent,
            subsequent_indent=indent,
        )
    return lines


def verification_report(issues: list[dict]) -> str:
    """The whole `verify` report for ``issues``, ending in its two summary lines.

    Every issue carrying instructions gets a block; the rest are counted and named on the
    closing line, so a register where nobody wrote any still says so rather than printing
    nothing at all.
    """
    lines: list[str] = []
    without: list[int] = []
    documented = 0
    for issue in issues:
        instructions = verification_instructions(issue)
        if not instructions:
            without.append(issue["number"])
            continue
        documented += 1
        if lines:
            lines.append("")
        lines.append(f"#{issue['number']}  {issue['title']}")
        lines.append("  How to verify:")
        lines += _wrap(instructions, "    ")
    if lines:
        lines.append("")
    noun = "finding" if documented == 1 else "findings"
    lines.append(f"{documented} {noun} with verification instructions.")
    if without:
        verb = "has" if len(without) == 1 else "have"
        named = ", ".join(f"#{n}" for n in without)
        lines.append(f"({len(without)} {verb} none: {named}.)")
    return "\n".join(lines)
