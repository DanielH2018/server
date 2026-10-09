"""The one table loop in the static-monitors template, as `parse_declared_monitors` reads it.

monitor-bridge's push tiles render from a loop over its check table (#3781):

    {% for <row> in lookup('file', playbook_dir ~ '<path>') | py_table('<TABLE>') %}

and the loop's entity line names each tile `{{ <row>.<field> | to_json }}`. A line-by-line
parser sees one templated name there, which is no monitor at all, so `TableLoop` reads the
table the loop iterates and hands back one name per row.
"""

import re
import sys as _sys
from pathlib import Path

from lib.repo_paths import FILTER_PLUGINS

_sys.path.insert(0, str(FILTER_PLUGINS))

from py_table import py_table

_LOOP_RE = re.compile(
    r"{%-?\s*for\s+(\w+)\s+in\s+lookup\('file',\s*playbook_dir\s*~\s*'([^']+)'\)"
    r"\s*\|\s*py_table\('(\w+)'\)"
)
_ENDFOR_RE = re.compile(r"{%-?\s*endfor\b")
_ROW_NAME_RE = re.compile(r'"name":\s*\{\{\s*(\w+)\.(\w+)\b')


class TableLoop:
    """Tracks whether the parser is inside the table loop, and the rows it iterates.

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
        elif _ENDFOR_RE.search(line):
            self.var, self.rows = None, []

    def names(self, line: str) -> list[str] | None:
        """One name per row when `line` names its entity from the loop variable, else None."""
        named = _ROW_NAME_RE.search(line)
        if self.var is None or not named or named.group(1) != self.var:
            return None
        return [row[named.group(2)] for row in self.rows]
