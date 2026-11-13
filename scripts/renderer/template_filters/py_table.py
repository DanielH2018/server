"""The filter reading a table of constructor calls out of a Python module's source.

monitor-bridge declares every check once, in `roles/k8s/monitor-bridge/files/check_table.py`,
as a tuple of `PushCheck(...)` calls (#3659). The pod imports that module. The generated
bridge fragments need the same rows, rendered on the controller, where importing the module
would mean importing every check body it names. So
this filter PARSES the source instead of running it:

    lookup('file', role_file('files/check_table.py', role='monitor-bridge'))
    | py_table('CHECKS')

returns one dict per call, holding each keyword argument whose value is a literal. A
non-literal value (`fn=service.check_n8n`) is left out, because it names code rather than
data. A positional argument raises, as does a missing table or an element that is not a call:
either would otherwise drop a row, and a dropped row is a check no generated fragment names.

`fragments_bridge.py` and the tests call this one function. `lib.ansible_jinja_env` registers it so a
`ValueError` fails the render as `FilterError("py_table: ...")` before anything
applies (#4505).
"""

import ast


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
