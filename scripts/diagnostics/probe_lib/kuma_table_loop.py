"""The table loop in the static-monitors template, as `parse_declared_monitors` reads it.

monitor-bridge's push tiles render from a loop over its check table (#3781):

    {% for <row> in lookup('file', playbook_dir ~ '<path>') | py_table('<TABLE>') %}

The loop's entity line names its tile `{{ <row>.<field> | to_json }}`. A line-by-line parser
sees one templated name there, which is no monitor at all, so `TableLoop` reads the rows the
loop iterates and hands back one name per row.

The loop may end in `if <row>.<field> not in ('a', 'b')`, which drops the rows named there.
M6 deletes a flux-owned bridge row's tile that way before it deletes the row (m6-retire-kuma.md,
"The cutover order"), and `TableLoop` skips the same rows, so `kuma-drift` does not name a
deleted tile missing.

The ingress tiles' loop over `containers_list` was the second such loop until M4's PR 4
(#5625) moved those probes to blackbox-exporter.
"""

import re

# `lib` sits under `scripts/`, which a module reached by a direct invocation does not have on
# sys.path; pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.repo_paths import FILTER_PLUGINS

_sys.path.insert(0, str(FILTER_PLUGINS))

from py_table import py_table

_LOOP_RE = re.compile(
    r"{%-?\s*for\s+(\w+)\s+in\s+lookup\('file',\s*playbook_dir\s*~\s*'([^']+)'\)"
    r"\s*\|\s*py_table\('(\w+)'\)"
)
_EXCLUDE_RE = re.compile(r"\bif\s+(\w+)\.(\w+)\s+not\s+in\s+\(([^)]*)\)")
_ENDFOR_RE = re.compile(r"{%-?\s*endfor\b")
_ROW_NAME_RE = re.compile(r'"name":\s*\{\{\s*(\w+)\.(\w+)\b')


class TableLoop:
    """Tracks whether the parser is inside a table loop, and the rows it iterates.

    Args:
      root: The `ansible/` directory the loop's `playbook_dir` path resolves under.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.var: str | None = None
        self.rows: list[dict] = []

    def see(self, line: str) -> None:
        """Enter the loop on its `{% for %}` line, and leave it on `{% endfor %}`."""
        if start := _LOOP_RE.search(line):
            source = (self.root / start.group(2).lstrip("/")).read_text()
            self.var, self.rows = start.group(1), py_table(source, start.group(3))
            if skip := _EXCLUDE_RE.search(line):
                if skip.group(1) == self.var:
                    dropped = set(re.findall(r"""['"]([^'"]+)['"]""", skip.group(3)))
                    self.rows = [
                        r for r in self.rows if r[skip.group(2)] not in dropped
                    ]
        elif _ENDFOR_RE.search(line):
            self.var, self.rows = None, []

    def names(self, line: str) -> list[str] | None:
        """One name per row when `line` names its entity from the loop variable, else None."""
        named = _ROW_NAME_RE.search(line)
        if self.var is None or not named or named.group(1) != self.var:
            return None
        return [row[named.group(2)] for row in self.rows]
