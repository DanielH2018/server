#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by inject-nested-docs.py for the payload arithmetic and the rule-glob parser
"""The arithmetic behind what `inject-nested-docs` delivers whole, with no hook constant.

`header` builds the line that opens a doc's block, and `inline_margins` measures the block a
whole doc would take against the limits it is given. The hook keeps its limits and its
preamble and passes them in, so this module never disagrees with the hook about the budget.
`rule_globs` reads a rule file's `paths:` globs, which decide the trigger a rule's header
names.
"""

import re

_QUOTES = "\"'"


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
