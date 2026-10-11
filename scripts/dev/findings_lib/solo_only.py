"""Which issues are solo-only: they cite the fan-out tooling, so no fan-out batch takes them.

`findings.py next` marks such an issue and `fanout.py place launch` refuses a batch holding
one (#3959). Kept out of `issue_model.py`, which sits at its module-length cap.
"""

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re

from dev.findings_lib.issue_model import _TRAILER_RE

# The fan-out pipeline's own code, as an issue body names it: any `fanout*.py` (the
# `fanout.py` entry point, its `fanout_place.py`-style shims, the `fanout-stop.py` hook) or any file
# under `fanout_lib/` at any depth, `fanout_lib/review/` included, with or without its directory. Bare names count because most of these
# issues cite `fanout_place.py` with no path, which `cited_paths` never captures. The
# lookbehind keeps `test_fanout_red_gate.py` out: a test of the tooling does not run a batch.
_FANOUT_TOOLING_RE = re.compile(
    r"(?<![\w-])(?:[\w.-]+/)*(?:fanout[\w-]*\.py|fanout_lib/(?:[\w.-]+/)*[\w.-]+\.\w+)(?![\w/])"
)


def fanout_tooling_paths(body: str) -> list[str]:
    """The fan-out tooling files an issue body cites, sorted; empty for ordinary work.

    An issue citing one is solo-only (#3959): `next` marks it and `fanout.py place launch`
    refuses a batch holding it. A batch that edits this code is reviewed, red-gated and
    stopped by its own edited copy, and fan-out batches on these files caused the most
    rework in 68 review records. A solo session claims and works it as usual.
    """
    text = _TRAILER_RE.sub("", (body or "").replace("\r\n", "\n"))
    return sorted({m.group(0) for m in _FANOUT_TOOLING_RE.finditer(text)})
