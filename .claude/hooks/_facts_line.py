#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by inject-nested-docs.py for the facts.lock line under a CLAUDE.md
"""The line `inject-nested-docs` puts under a `CLAUDE.md`'s header: its unverified sections.

WHAT. `facts_line` names the doc's sections that cite the tree and have no row in
`docs/facts.lock`: the sections `scripts/dev/fact_status.py status` grades UNVERIFIED. A
session reading the doc through Bash then knows which sections no `verify` has checked. A
section that cites nothing is a convention, which the lock has nothing to check, so it is
never named; a doc with nothing to name gets no line. The line says only what the lock lacks,
never that a section with a row is true. `.claude/rules/facts.md` defines the statuses.

CHEAP BY DESIGN. Nothing here hashes an atom. Master CI fails on a section whose recorded
hashes moved, so the hook reads the lock's keys, `git ls-files` and the doc, and no more.

ONE GRAMMAR. Sections and citations come from `scripts/lib/facts/citations.py` (stdlib only),
imported deferred and guarded the way `session-health.py` imports `lib`: only a `CLAUDE.md`
injection pays for the import, and any failure returns no line rather than stopping the
injection the line rides on.
"""

import json
import os
import sys
from pathlib import Path

# The lock `fact_status.py verify` writes, one row per verified section.
FACTS_LOCK = os.path.join("docs", "facts.lock")

# How many section names the line spells out before it says "(+N more)".
MAX_NAMES = 6

# This checkout's `scripts/`, the root `lib.facts` imports from.
_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
)


def _lock_units(root):
    """The section keys `docs/facts.lock` holds a row for, read as `lock.read_lock` reads it.

    Not `read_lock` itself: `lib.facts.lock` imports the atom-hashing modules, about 25 ms of
    import for a reader that needs only the keys.
    """
    with open(os.path.join(root, FACTS_LOCK), encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if not ln.startswith("#")]
    return frozenset(json.loads("\n".join(lines))["units"])


def unverified_sections(root, doc, cache=None):
    """Headings of `doc`'s citing sections with no lock row, in doc order, or None.

    None for a doc outside the store (anything but a tracked `CLAUDE.md`), and on any failure
    to import `lib.facts` or to read the lock or git. `cache` is shared across one command's
    docs, so git and the lock are read once per checkout.
    """
    if os.path.basename(doc) != "CLAUDE.md":
        return None
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    try:
        from lib.facts.citations import (
            in_tree,
            parse_citations,
            sections,
            tracked_files,
        )

        cache = {} if cache is None else cache
        if root not in cache:
            cache[root] = (tracked_files(Path(root)), _lock_units(root))
        tracked, units = cache[root]
        key = doc.replace(os.sep, "/")
        if key not in tracked:
            return None
        text = Path(root, doc).read_text(encoding="utf-8")
        return [
            sec.heading
            for sec in sections(key, text)
            if sec.key not in units
            and any(in_tree(c, tracked) for c in parse_citations(sec.body)[0])
        ]
    except Exception:
        return None


def facts_line(root, doc, cache=None):
    """`facts.lock: not verified: "<heading>", ... (+N more)` for `doc`, or None for no line."""
    missing = unverified_sections(root, doc, cache)
    if not missing:
        return None
    names = [f'"{h}"' if h else "(text before the first heading)" for h in missing]
    more = len(names) - MAX_NAMES
    tail = f" (+{more} more)" if more > 0 else ""
    return f"facts.lock: not verified: {', '.join(names[:MAX_NAMES])}{tail}"
