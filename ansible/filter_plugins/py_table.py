"""Ansible filter plugin reading a table of constructor calls out of a Python module's source.

monitor-bridge declares every check once, in `roles/k8s/monitor-bridge/files/check_table.py`,
as a tuple of `PushCheck(...)` calls (#3659). The pod imports that module. Two templates need
the same rows: the bridge's env-secret and uptime-kuma's static monitors, each rendered on the
controller, where importing the module would mean importing every check body it names. So
this filter PARSES the source instead of running it:

    lookup('file', playbook_dir ~ '/roles/k8s/monitor-bridge/files/check_table.py')
    | py_table('CHECKS')

returns one dict per call, holding each keyword argument whose value is a literal. A
non-literal value (`fn=host.check_disk`) is left out, because it names code rather than
data. A positional argument raises, as does a missing table or an element that is not a call:
either would otherwise drop a row, and a dropped row is a check pushing nowhere or a Kuma tile
AutoKuma deletes.

No Ansible import at load time, so the tests call the same function the playbook runs.
`FilterModule` registers it through `_as_filter`, which re-raises a `ValueError` as
`AnsibleFilterError("py_table: ...")` and fails the render before anything applies (#4505).
"""

import ast

import sys as _sys
from pathlib import Path as _Path

# Ansible loads a plugin by file path, so `filter_errors` beside it imports by bare name only
# once this directory is on the path (#4527).
if str(_Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))

from filter_errors import _as_filter


def _literal(node: ast.expr) -> tuple[bool, object]:
    try:
        return True, ast.literal_eval(node)
    except ValueError:
        return False, None


def py_table(source: str, name: str) -> list[dict]:
    """The keyword-literal arguments of each call in the tuple or list assigned to `name`.

    Args:
        source: A Python module's text.
        name: The module-level name the table is assigned to, with or without an annotation.

    Returns:
        One dict per element, in source order.

    Raises:
        ValueError: `name` is not assigned a tuple or list at module level, an element is not
            a call, or a call passes a positional argument.
    """
    table = None
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            table = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            table = node.value
    if not isinstance(table, ast.Tuple | ast.List):
        raise ValueError(f"no tuple or list is assigned to {name}")
    rows = []
    for i, call in enumerate(table.elts):
        if not isinstance(call, ast.Call):
            raise ValueError(f"{name}[{i}] is not a call")
        if call.args:
            raise ValueError(f"{name}[{i}] passes a positional argument")
        row = {}
        for kw in call.keywords:
            ok, value = _literal(kw.value)
            if kw.arg is not None and ok:
                row[kw.arg] = value
        rows.append(row)
    return rows


class FilterModule:
    def filters(self):
        return {"py_table": _as_filter(py_table)}
