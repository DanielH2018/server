"""The reconcile stub starts without its slow imports, which a run against a real manifest pays ~800 times.

`json` and `fnmatch` import `re`. With `pathlib`, they took a stub call from about 9 ms to
about 17 ms (#8583).
"""

import sys

from _reconcile_host import STUB
from lib.proc_testing import run

DEFERRED = ("re", "json", "fnmatch", "pathlib")


def test_importing_the_stub_loads_none_of_the_deferred_modules(tmp_path):
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(STUB.parent)!r})\n"
        f"import {STUB.stem}\n"
        f"print(','.join(m for m in {DEFERRED!r} if m in sys.modules))\n"
    )
    result = run(
        [sys.executable, "-S", "-c", probe],
        env={"STUB_STATE": str(tmp_path), "HOMELAB_ROOT": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
