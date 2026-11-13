"""The printed-JSON census row: a tool's `--json` output carries no indentation.

`.claude/rules/tool-output.md` holds the convention this row enforces one part of. The row sits
in its own module because `test_census_rows_python.py`, which imports it into `ROWS`, is at the
cap for a test module.
"""

import re

from _row_table import Census, Subject, lines_matching, tracked

PRINTED_JSON_IS_COMPACT = Census(
    name="printed-json-is-compact",
    reason=(
        "A tool's `--json` output is read by `jq` and agents only, and `indent=` roughly "
        "doubles its tokens, so moving a skill to `--json` cost context instead of saving it "
        "(#7892). Print `json.dumps(rows)`. A file written for a person to diff keeps its "
        "indent. `.claude/rules/tool-output.md` has the rest of the convention."
    ),
    files=lambda: [
        rel
        for rel in tracked("scripts/*.py", "hosts/*.py", ".claude/hooks/*.py")
        if "/tests/" not in rel
    ],
    offence=lines_matching(re.compile(r"print\(json\.dumps\([^\n]*\bindent=")),
    red=(
        Subject("a.py", "    print(json.dumps(rows, indent=2))\n"),
        Subject("b.py", "print(json.dumps(m, indent=2) if ns.json else fmt(m))\n"),
    ),
    green=(
        Subject("a.py", "    print(json.dumps(rows))\n"),
        Subject("b.py", "path.write_text(json.dumps(record, indent=2))\n"),
    ),
    min_matches=400,
    must_find=frozenset({"scripts/dev/findings.py", "scripts/dev/fanout_lib/place.py"}),
)
