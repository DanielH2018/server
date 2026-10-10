"""Doc fragments for the monitor-bridge pages (docs/monitor-bridge-checks.md, docs/monitor-bridge-internals.md).

Each entry in `FRAGMENTS` builds one fragment the way `gen_doc_fragments.py` builds its own:
a reader that parses the tree statically, and a pure renderer from what it returned to
markdown. `gen_doc_fragments.FRAGMENTS` merges this table, prepends the provenance header and
writes the file, so a builder returns only `(body, sources)`.
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

# name -> () -> (body, sources). The name is the file stem a page includes.
FRAGMENTS: dict[str, Callable[[], tuple[str, list[str]]]] = {}
