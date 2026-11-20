"""`affected_lib/record.py` against a real `strace` run of a real module.

The parser and the launcher are only worth anything against what the kernel reports, so this
test runs `strace` itself. It needs `strace` on the host, and it skips under
`affected_tests.py record`: a process already under ptrace cannot start a tracer of its own.
That skip leaves this module with no row, so the selection runs it on every pull request. It
lives apart from `test_affected_record.py` so that module keeps its row.

Run: uv run pytest scripts/dev/tests/test_affected_record_traced.py
"""

import shutil
from pathlib import Path

import pytest
from dev.affected_lib import record, table

from _helpers import REPO

TRACE_TEST = "scripts/dev/tests/test_affected_trace.py"
TABLE_TEST = "scripts/dev/tests/test_affected_table.py"


def _already_traced() -> bool:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("TracerPid:"):
            return line.split()[1] != "0"
    return False


@pytest.mark.skipif(shutil.which("strace") is None, reason="needs strace on the host")
@pytest.mark.skipif(_already_traced(), reason="already traced, strace cannot attach")
def test_a_traced_run_records_what_the_module_read():
    blobs = table.tracked_blobs(REPO)
    git_dir = record.git("rev-parse", "--absolute-git-dir", cwd=REPO).stdout.strip()

    row = record.record_module(TRACE_TEST, REPO, frozenset(blobs), f"{git_dir}/index")

    assert isinstance(row, table.Row), row
    assert {
        TRACE_TEST,
        "scripts/dev/affected_lib/trace.py",
        "pyproject.toml",
    } <= row.files
    assert TABLE_TEST not in row.files
    # pytest's collector and the import system listed these two, and the module did not.
    assert not {"scripts/dev/tests", "scripts/dev"} & row.dirs
    assert "scripts/dev/tests/__init__.py" in row.missing
    assert not row.tree
