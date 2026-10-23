"""Read one Markdown section, its numbered steps and its commands, for the runbook text tests.

The Flux runbooks in docs/flux-deploy-plane.md and the three cold-start runbooks that link to
them are operator-only, so their tests read them as text (D5 step 29). A fenced code block's
lines are never headings, so a `#` comment in a shell block cannot end a section early.
"""

import re

_FENCE = re.compile(r"^\s*(```|~~~)")
_STEP = re.compile(r"^(\d+)\. ")


def section(text: str, heading: str) -> str:
    """The lines under the heading line equal to `heading`, up to the next heading at its level.

    Raises:
        AssertionError: no line equals `heading`, so a renamed heading fails the test rather
            than leaving every check to pass over an empty string.
    """
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line == heading), None)
    assert start is not None, f"no heading {heading!r}"
    end_heading = re.compile(r"^#{1,%d} " % level)
    in_fence = False
    for i in range(start + 1, len(lines)):
        if _FENCE.match(lines[i]):
            in_fence = not in_fence
        elif not in_fence and end_heading.match(lines[i]):
            return "\n".join(lines[start + 1 : i])
    return "\n".join(lines[start + 1 :])


def command_lines(text: str) -> list[str]:
    """Every line of `text`, stripped, with a trailing-backslash continuation joined to it."""
    out: list[str] = []
    pending = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        out.append(pending + stripped)
        pending = ""
    if pending:
        out.append(pending.rstrip())
    return out


def step_spans(text: str) -> list[tuple[int, int, int]]:
    """`(number, first line, end line)` of each top-level numbered step in `text`'s lines.

    A step runs from its `N. ` line to the next step, or to the first line at column 0 that is
    neither blank nor a step, such as the paragraph after the list.
    """
    lines = text.splitlines()
    starts = [
        (i, int(m.group(1))) for i, line in enumerate(lines) if (m := _STEP.match(line))
    ]
    spans = []
    for k, (i, number) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        for j in range(i + 1, end):
            if lines[j] and not lines[j][0].isspace():
                end = j
                break
        spans.append((number, i, end))
    return spans


def steps(text: str) -> dict[int, str]:
    """Each top-level numbered step's text, by its number (`step_spans`)."""
    lines = text.splitlines()
    return {n: "\n".join(lines[a:b]) for n, a, b in step_spans(text)}


def normalised(text: str) -> str:
    """`text` with every run of whitespace collapsed to one space."""
    return " ".join(text.split())
