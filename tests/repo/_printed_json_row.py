"""The printed-JSON census row: a tool's `--json` output carries no indentation.

`.claude/rules/tool-output.md` holds the convention this row enforces one part of. The row sits
in its own module because `test_census_rows_python.py`, which imports it into `ROWS`, is at the
cap for a test module.
"""

import ast

from _row_table import Census, Subject, tracked


def _has_indent(call: ast.Call) -> bool:
    return any(k.arg == "indent" for k in call.keywords)


def _is_stdout(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "stdout"


def _printed_with_indent(subject: Subject) -> list[str]:
    """Each `print(...)` holding an indented `json.dumps`, and each indented `json.dump` to stdout.

    Reading the syntax tree, not lines, catches a call whose `indent=` sits on a later line.
    """
    try:
        tree = ast.parse(subject.text)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name == "print":
            printed = any(
                isinstance(inner, ast.Call)
                and ast.unparse(inner.func).endswith("json.dumps")
                and _has_indent(inner)
                for arg in node.args
                for inner in ast.walk(arg)
            )
            if printed:
                hits.append(f"line {node.lineno}: print of json.dumps with indent=")
        elif name.endswith("json.dump") and _has_indent(node):
            if len(node.args) > 1 and _is_stdout(node.args[1]):
                hits.append(f"line {node.lineno}: json.dump to stdout with indent=")
    return hits


PRINTED_JSON_IS_COMPACT = Census(
    name="printed-json-is-compact",
    reason=(
        "A tool's `--json` output is read by `jq` and agents only, and `indent=` roughly "
        "doubles its tokens, so moving a skill to `--json` cost context instead of saving it "
        "(#7892). Print `json.dumps(rows)`, or `json.dump(rows, sys.stdout)`. A file written "
        "for a person to diff keeps its indent. `.claude/rules/tool-output.md` has the rest "
        "of the convention."
    ),
    files=lambda: [
        rel
        for rel in tracked("scripts/*.py", "hosts/*.py", ".claude/hooks/*.py")
        if "/tests/" not in rel
    ],
    offence=_printed_with_indent,
    red=(
        Subject("a.py", "print(json.dumps(rows, indent=2))\n"),
        Subject("b.py", "print(json.dumps(m, indent=2) if ns.json else fmt(m))\n"),
        Subject("c.py", "json.dump(model, sys.stdout, indent=2)\n"),
        Subject(
            "d.py",
            "print(\n    json.dumps(\n        row,\n        indent=2,\n    )\n)\n",
        ),
    ),
    green=(
        Subject("a.py", "print(json.dumps(rows))\n"),
        Subject("b.py", "path.write_text(json.dumps(record, indent=2))\n"),
        Subject("c.py", "json.dump(record, handle, indent=2)\n"),
        Subject(
            "d.py",
            "body = json.dumps(\n    record,\n    indent=2,\n)\nprint(len(body))\n",
        ),
    ),
    min_matches=400,
    must_find=frozenset({"scripts/dev/findings.py", "scripts/dev/fanout_lib/place.py"}),
)
