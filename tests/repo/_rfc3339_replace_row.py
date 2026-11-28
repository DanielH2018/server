"""The RFC 3339 census row: a stamp is parsed by `fromisoformat` with no `Z` replacement.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import re

from _row_table import Census, Subject, lines_matching, tracked

SELF = "tests/repo/_rfc3339_replace_row.py"

Z_REPLACEMENT = re.compile(r"""\.replace\(\s*["']Z["']\s*,\s*["']\+00:00["']\s*\)""")

NO_Z_REPLACEMENT = Census(
    name="rfc3339-reads-need-no-z-replacement",
    reason=(
        "`datetime.fromisoformat` takes a trailing `Z` since Python 3.11, so "
        '`.replace("Z", "+00:00")` before it does nothing. Parse the stamp, then refuse a '
        "result with no tzinfo: a bare date parses as naive and `.timestamp()` reads it as local time."
    ),
    files=lambda: [rel for rel in tracked("*.py") if rel != SELF],
    offence=lines_matching(Z_REPLACEMENT),
    red=(Subject("a.py", 'datetime.fromisoformat(s.replace("Z", "+00:00"))'),),
    green=(Subject("a.py", "datetime.fromisoformat(s)"),),
    min_matches=100,
    must_find=frozenset({"hosts/lib/host_lib.py"}),
    allow={
        "k8s/roles/karakeep/files/karakeep_archive_pruner.py": (
            "role payload, lands one role per PR in #10312"
        ),
    },
)
