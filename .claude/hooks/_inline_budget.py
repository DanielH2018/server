#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by inject-nested-docs.py for the payload arithmetic, the rule-glob parser and its --report mode
"""The arithmetic behind what `inject-nested-docs` delivers whole, with no hook constant.

`header` builds the line that opens a doc's block, and `inline_margins` measures the block a
whole doc would take against the limits it is given. The hook keeps its limits and its
preamble and passes them in, so this module never disagrees with the hook about the budget.
`rule_globs` reads a rule file's `paths:` globs, which decide the trigger a rule's header
names.

`report_rows` and `print_report` are the hook's `--report` mode. They list every instruction
file the hook serves, with its margin under each limit and whether the hook delivers it whole
or as a head. They measure with `inline_margins`, so the report and the delivery share one
computation.
"""

import os
import re
import subprocess
import sys
from pathlib import PurePosixPath
from typing import NamedTuple

_QUOTES = "\"'"

# A `whole` row this close to a limit is marked `near`. The character mark covers a deeper
# trigger, which lengthens the header. The line mark covers growth only, because the header
# is always one line.
NEAR_CHARS = 200
NEAR_LINES = 10

# The harness loads the root `CLAUDE.md` whole at session start, and Anthropic recommends
# keeping it under 200 lines.
ROOT_TARGET_LINES = 200


def header(doc, trigger):
    """The line that opens `doc`'s block, naming the path that triggered it."""
    return f"===== {doc} (applies to `{trigger}`) ====="


def inline_margins(text, header, max_chars, max_lines):
    """The (chars, newlines) left under the two limits by `text`'s whole-doc block.

    The block is the one the hook renders for a doc delivered whole: the header, a newline,
    `text.rstrip()` and a newline. A margin is negative when the block is over that limit.
    """
    block = f"{header}\n{text.rstrip()}\n"
    return max_chars - len(block), max_lines - block.count("\n")


def rule_globs(rule_text):
    """The `paths:` globs in a rule file's YAML frontmatter, parsed without a YAML library.

    The block-list form is what this repo's three rule files use; the flow form is accepted
    so a fourth written that way still matches.
    """
    if not rule_text.startswith("---"):
        return []
    end = rule_text.find("\n---", 3)
    frontmatter = rule_text[3:end] if end != -1 else rule_text[3:]
    globs, in_paths = [], False
    for line in frontmatter.splitlines():
        flow = re.match(r"^paths:\s*\[(.*)\]\s*$", line)
        if flow:
            globs.extend(
                g.strip().strip(_QUOTES) for g in flow.group(1).split(",") if g.strip()
            )
            continue
        if re.match(r"^paths:\s*$", line):
            in_paths = True
            continue
        if in_paths:
            item = re.match(r"^\s+-\s*(.+?)\s*$", line)
            if item:
                globs.append(item.group(1).strip(_QUOTES))
                continue
            if line.strip():
                in_paths = False
    return globs


# ── the --report mode ────────────────────────────────────────────────────────────────


class Row(NamedTuple):
    """One instruction file as the report measures it.

    `form` is `whole`, `head`, or `none` for a rule whose globs match no tracked path. A
    `none` row has no margins, because the hook never injects it.
    """

    doc: str
    chars: int
    lines: int
    form: str
    chars_left: int | None
    lines_left: int | None
    near: bool


def _tracked(root):
    """Every tracked path under `root`. Raises `CalledProcessError` outside a git checkout."""
    # An inherited GIT_DIR outranks -C, so the caller's git env must not leak in.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout
    return [path for path in out.split("\0") if path]


def _rule_trigger(rule_text, tracked):
    """The shortest tracked path the rule's `paths:` globs match, or None.

    The match is the `full_match` call `docs_for` makes. The result is representative, not a
    bound: a session can name a shorter directory or an untracked path.
    """
    globs = rule_globs(rule_text)
    hits = [p for p in tracked if any(PurePosixPath(p).full_match(g) for g in globs)]
    return min(hits, key=lambda p: (len(p), p)) if hits else None


def _row(doc, text, trigger, max_chars, max_lines):
    chars, lines = len(text), len(text.splitlines())
    if trigger is None:
        return Row(doc, chars, lines, "none", None, None, False)
    chars_left, lines_left = inline_margins(
        text, header(doc, trigger), max_chars, max_lines
    )
    whole = chars_left >= 0 and lines_left >= 0
    near = whole and (chars_left < NEAR_CHARS or lines_left < NEAR_LINES)
    form = "whole" if whole else "head"
    return Row(doc, chars, lines, form, chars_left, lines_left, near)


def report_rows(root, max_chars, max_lines):
    """One `Row` per tracked non-root `CLAUDE.md` and `.claude/rules/*.md`, tightest first.

    Args:
        root: The checkout to walk.
        max_chars, max_lines: The limits a doc's block is weighed against, which are the
            hook's payload less its preamble.

    A `CLAUDE.md` is measured with its own directory as the trigger. That is the shortest
    header the hook can build for it, so `head` holds on every trigger. Rows sort by the
    tighter margin as a fraction of its limit, so heads come first and `none` rows last.

    Raises:
        CalledProcessError: `root` is not a git checkout.
        RuntimeError: the walk found no `CLAUDE.md` besides the root one, which an empty
            table would otherwise report as clean.
    """
    tracked = _tracked(root)
    claude_docs = [
        p for p in tracked if p != "CLAUDE.md" and PurePosixPath(p).name == "CLAUDE.md"
    ]
    if not claude_docs:
        raise RuntimeError(f"no tracked CLAUDE.md besides the root one under {root}")
    rules = [p for p in tracked if PurePosixPath(p).full_match(".claude/rules/*.md")]
    rows = []
    for doc in claude_docs + rules:
        with open(os.path.join(root, doc), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if doc in rules:
            trigger = _rule_trigger(text, tracked)
        else:
            trigger = str(PurePosixPath(doc).parent)
        rows.append(_row(doc, text, trigger, max_chars, max_lines))

    def tightness(row):
        if row.form == "none":
            return (1, 0.0, row.doc)
        return (0, min(row.chars_left / max_chars, row.lines_left / max_lines), row.doc)

    return sorted(rows, key=tightness)


def print_report(root, payload_chars, payload_lines, preamble, out=sys.stdout):
    """Print the budget line, one line per `report_rows` row, then the root `CLAUDE.md`.

    The first line states the payload limits and what the preamble leaves of them, both
    computed from the hook's own constants. The root `CLAUDE.md` is reported by its line
    count alone, because the harness loads it whole and the hook never injects it.
    """
    max_chars = payload_chars - len(preamble)
    max_lines = payload_lines - preamble.count("\n")
    rows = report_rows(root, max_chars, max_lines)
    with open(
        os.path.join(root, "CLAUDE.md"), encoding="utf-8", errors="replace"
    ) as fh:
        root_lines = len(fh.read().splitlines())
    print(
        f"budgets: payload {payload_chars} chars / {payload_lines} lines; "
        f"a doc's block gets {max_chars} chars / {max_lines} newlines after the preamble",
        file=out,
    )
    print(
        f"{'chars':>6} {'lines':>5} {'form':<5} {'chars_left':>10} {'lines_left':>10} "
        f"{'near':<4} doc",
        file=out,
    )
    for row in rows:
        chars_left = "-" if row.chars_left is None else row.chars_left
        lines_left = "-" if row.lines_left is None else row.lines_left
        near = "near" if row.near else "-"
        print(
            f"{row.chars:>6} {row.lines:>5} {row.form:<5} {chars_left:>10} "
            f"{lines_left:>10} {near:<4} {row.doc}",
            file=out,
        )
    print(
        f"root CLAUDE.md: {root_lines} lines, against a target of under "
        f"{ROOT_TARGET_LINES} (loaded whole at session start, never injected)",
        file=out,
    )
