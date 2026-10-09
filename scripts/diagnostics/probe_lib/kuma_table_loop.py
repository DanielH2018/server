"""The two table loops in the static-monitors template, as `parse_declared_monitors` reads them.

monitor-bridge's push tiles render from a loop over its check table (#3781):

    {% for <row> in lookup('file', playbook_dir ~ '<path>') | py_table('<TABLE>') %}

and the ingress tiles from a loop over daniel-box's `containers_list` (#3690):

    {% for <row> in containers_list | kuma_ingress_monitors %}

Each loop's entity line names its tile `{{ <row>.<field> | to_json }}`. A line-by-line
parser sees one templated name there, which is no monitor at all, so `TableLoop` reads the
rows the loop iterates and hands back one name per row. The ingress rows come from the same
filter the playbook runs, applied to the inventory file the template's `containers_list`
comes from.
"""

import re

# `lib` sits under `scripts/`, which a module reached by a direct invocation does not have on
# sys.path; pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.repo_paths import FILTER_PLUGINS

_sys.path.insert(0, str(FILTER_PLUGINS))

from kuma_monitors import kuma_ingress_monitors
from lib import yaml_fast
from py_table import py_table

_LOOP_RE = re.compile(
    r"{%-?\s*for\s+(\w+)\s+in\s+lookup\('file',\s*playbook_dir\s*~\s*'([^']+)'\)"
    r"\s*\|\s*py_table\('(\w+)'\)"
)
_INGRESS_LOOP_RE = re.compile(
    r"{%-?\s*for\s+(\w+)\s+in\s+containers_list\s*\|\s*kuma_ingress_monitors\b"
)
# The host whose `containers_list` the uptime-kuma role renders against.
_INGRESS_HOST_VARS = "inventory/host_vars/daniel-box.yml"
_ENDFOR_RE = re.compile(r"{%-?\s*endfor\b")
_ROW_NAME_RE = re.compile(r'"name":\s*\{\{\s*(\w+)\.(\w+)\b')


class TableLoop:
    """Tracks whether the parser is inside a table loop, and the rows it iterates.

    Args:
      root: The `ansible/` directory the loop's `playbook_dir` path, and the inventory, resolve
        under.
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
        elif start := _INGRESS_LOOP_RE.search(line):
            host_vars = yaml_fast.safe_load(
                (self.root / _INGRESS_HOST_VARS).read_text()
            )
            self.var = start.group(1)
            self.rows = kuma_ingress_monitors(host_vars["containers_list"])
        elif _ENDFOR_RE.search(line):
            self.var, self.rows = None, []

    def names(self, line: str) -> list[str] | None:
        """One name per row when `line` names its entity from the loop variable, else None."""
        named = _ROW_NAME_RE.search(line)
        if self.var is None or not named or named.group(1) != self.var:
            return None
        return [row[named.group(2)] for row in self.rows]
