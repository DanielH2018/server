"""The row harness for operator-only runbooks: one `RunbookRule` per ordering or flag rule.

A runbook step that needs `sudo k3s kubectl` or the `DEPLOY_HOLD` variable cannot run in a
test, so its guard reads the document as text. Each guard was a section reader, a command
reader and an order rule written out by hand. As a row, the section, the unit and the rule are
data, and the reading, the verdict and the red proof are here once.

A rule reads the text under `heading` in `doc`, splits it into units (`LINES`, `COMMANDS`,
`STEPS`, `ITEMS` or `BLOCKS`), keeps the units `select` matches, and holds when:

- each kept unit carries every string in `require`;
- no kept unit matches `forbid`;
- a unit matching `precede` comes before the first kept unit.

The verdict is `_row_table.verdict`, so a heading that went, or a selector that keeps nothing,
fails as `subject gone: delete this row`. Every rule carries `red`: edits of its own section
that the rule must flag. The real section is its green half.

Run: uv run pytest tests/repo/census_rows/test_runbook_rules.py
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from _row_table import REPO, verdict

_FENCE = re.compile(r"^\s*(```|~~~)")
_FENCED_BLOCK = re.compile(
    r"^[ \t]*(?:```|~~~)[^\n]*\n(.*?)^[ \t]*(?:```|~~~)", re.MULTILINE | re.DOTALL
)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_STEP = re.compile(r"^(\d+)\. ")
_LIST_ITEM = re.compile(r"^\s*(?:-|\d+\.)\s")


def section(text: str, heading: str) -> str:
    """The lines under the heading line equal to `heading`, up to the next heading at its level.

    A fenced block's lines are never headings, so a `#` comment in a shell block cannot end
    the section early.

    Raises:
        LookupError: no line equals `heading`.
    """
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line == heading), None)
    if start is None:
        raise LookupError(f"no heading {heading!r}")
    end_heading = re.compile(r"^#{1,%d} " % level)
    in_fence = False
    for i in range(start + 1, len(lines)):
        if _FENCE.match(lines[i]):
            in_fence = not in_fence
        elif not in_fence and end_heading.match(lines[i]):
            return "\n".join(lines[start + 1 : i])
    return "\n".join(lines[start + 1 :])


def normalised(text: str) -> str:
    return " ".join(text.split())


# ── Units: how a rule splits its section ─────────────────────────────────────────────

Unit = Callable[[str], list[str]]


def LINES(text: str) -> list[str]:
    """Every line, prose and fenced alike, stripped, with a `\\` continuation joined to it."""
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


def COMMANDS(text: str) -> list[str]:
    """Every command the text shows, in document order, each normalised to one line.

    A fenced block gives one command per logical line, its `\\` continuations joined. An
    inline code span gives one command, with the line breaks Markdown wraps it across
    collapsed.
    """
    found: list[str] = []
    cursor = 0
    for block in _FENCED_BLOCK.finditer(text):
        found += [
            normalised(s) for s in _INLINE_CODE.findall(text[cursor : block.start()])
        ]
        found += [
            normalised(line)
            for line in block.group(1).replace("\\\n", " ").splitlines()
            if line.strip()
        ]
        cursor = block.end()
    found += [normalised(s) for s in _INLINE_CODE.findall(text[cursor:])]
    return found


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


def STEPS(text: str) -> list[str]:
    """Each top-level numbered step's text, normalised, in order (`step_spans`)."""
    lines = text.splitlines()
    return [normalised("\n".join(lines[a:b])) for _, a, b in step_spans(text)]


def ITEMS(text: str) -> list[str]:
    """Each list item's own text, without its nested items, plus each plain paragraph."""
    items: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            items.append([])
        elif _LIST_ITEM.match(line) or not items:
            items.append([line])
        else:
            items[-1].append(line)
    return [normalised(" ".join(item)) for item in items if item]


def BLOCKS(text: str) -> list[str]:
    """Each list item with every blank or deeper-indented line under it, normalised.

    A nested item or an indented paragraph belongs to the item above it, so a rule that
    judges a bullet also judges what hangs from it. A nested item is also a block of its own,
    and an outer item's block holds the inner ones.
    """
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        if not _LIST_ITEM.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        end = i + 1
        while end < len(lines) and (
            not lines[end].strip()
            or len(lines[end]) - len(lines[end].lstrip()) > indent
        ):
            end += 1
        blocks.append(normalised("\n".join(lines[i:end])))
    return blocks


# ── Red fixtures: edits of the rule's own section ────────────────────────────────────

Red = Callable[[str], str]


def edit(old: str, new: str, *, every: bool = False) -> Red:
    """The section with `old` replaced once, or everywhere. A missing `old` fails the proof."""

    def red(text: str) -> str:
        if old not in text:
            raise LookupError(f"red fixture anchor {old!r} is gone")
        return text.replace(old, new, -1 if every else 1)

    return red


def edit_words(words: str, new: str) -> Red:
    """The section with the one run of `words`, across any line wrapping, replaced."""
    pattern = r"\s+".join(re.escape(word) for word in words.split())

    def red(text: str) -> str:
        out, count = re.subn(pattern, new, text)
        if count != 1:
            raise LookupError(f"red fixture words {words!r} matched {count} times")
        return out

    return red


def move_step(step: int, before: int) -> Red:
    """The section with numbered step `step` moved to just before step `before`."""

    def red(text: str) -> str:
        lines = text.splitlines()
        spans = {n: (a, b) for n, a, b in step_spans(text)}
        order = [n for n in spans if n != step]
        order.insert(order.index(before), step)
        first, last = (
            min(a for a, _ in spans.values()),
            max(b for _, b in spans.values()),
        )
        body = [line for n in order for line in lines[slice(*spans[n])]]
        return "\n".join(lines[:first] + body + lines[last:])

    return red


def move_line_after(moved: re.Pattern[str], anchor: re.Pattern[str]) -> Red:
    """The section with the first line `moved` matches put just after the first `anchor` line."""

    def red(text: str) -> str:
        lines = text.splitlines()
        line = lines.pop(next(i for i, s in enumerate(lines) if moved.search(s)))
        lines.insert(next(i for i, s in enumerate(lines) if anchor.search(s)) + 1, line)
        return "\n".join(lines)

    return red


# ── The row ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunbookRule:
    name: str
    reason: str
    doc: str
    # The section's heading, then each nested heading within it, outermost first.
    heading: tuple[str, ...]
    unit: Unit
    red: tuple[Red, ...]
    # The units the rule judges; None judges every unit.
    select: re.Pattern[str] | None = None
    require: tuple[str, ...] = ()
    forbid: re.Pattern[str] | None = None
    precede: re.Pattern[str] | None = None
    min_matches: int = 1

    def text(self) -> str:
        """The rule's section of the tracked document.

        Raises:
            LookupError: a heading is gone.
        """
        text = (REPO / self.doc).read_text()
        for heading in self.heading:
            text = section(text, heading)
        return text

    def problems(self, text: str) -> list[str]:
        """Every problem the rule finds in `text`, a section of `doc`, or [] when it holds."""
        units = self.unit(text)
        kept = [
            i
            for i, unit in enumerate(units)
            if self.select is None or self.select.search(unit)
        ]
        offenders = []
        for i in kept:
            if missing := [need for need in self.require if need not in units[i]]:
                offenders.append(f"lacks {missing}: {units[i][:200]}")
            if self.forbid and (hit := self.forbid.search(units[i])):
                offenders.append(
                    f"holds the forbidden {hit.group(0)!r}: {units[i][:200]}"
                )
        if self.precede and kept:
            before = next(
                (i for i, unit in enumerate(units) if self.precede.search(unit)), None
            )
            if before is None or before > kept[0]:
                offenders.append(
                    f"nothing matching {self.precede.pattern!r} comes before "
                    f"{units[kept[0]][:200]!r}"
                )
        return verdict(
            self.name,
            self.reason,
            {self.doc: len(kept)} if kept else {},
            [(self.doc, line) for line in offenders],
            min_matches=self.min_matches,
            must_find=frozenset(),
            allow={},
        )


def check(rule: RunbookRule) -> list[str]:
    """Every problem `rule` has over its tracked document, or []."""
    try:
        text = rule.text()
    except LookupError as gone:
        return [f"{rule.name}: subject gone: delete this row ({gone})"]
    return rule.problems(text)


def proof_problems(rule: RunbookRule) -> list[str]:
    """Each red fixture the rule passes, or that leaves the section as it was."""
    if not rule.red:
        return ["a rule needs at least one red fixture"]
    text = rule.text()
    problems = []
    for n, red in enumerate(rule.red):
        fixture = red(text)
        if fixture == text:
            problems.append(f"red {n} leaves the section unchanged")
        elif not rule.problems(fixture):
            problems.append(f"red {n} passed")
    return problems
