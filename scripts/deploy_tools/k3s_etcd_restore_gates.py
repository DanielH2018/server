#!/usr/bin/env python3
"""Forwarding shim: `runbook_gates.py etcd-restore`, under the path it had before #4344.

The etcd restore gates moved into `runbook_gates_lib/etcd_restore.py` behind
`scripts/deploy_tools/runbook_gates.py`. This path survives because
`ansible/roles/setup/k3s/defaults/main.yml` names it, and that is the bring-up k3s role,
which only a hand apply reaches. Retire the shim in the change that next edits that file.

Usage:
    uv run python scripts/deploy_tools/k3s_etcd_restore_gates.py [--gate N] <snapshot-name>
"""

import runpy
import sys
from pathlib import Path as _Path

if __name__ == "__main__":
    # Run as a script rather than imported, so runbook_gates.py keeps its own entry-point
    # bootstrap and the scripts reference page still lists it as one a person runs.
    sys.argv[1:1] = ["etcd-restore"]
    runpy.run_path(
        str(_Path(__file__).resolve().parent / "runbook_gates.py"), run_name="__main__"
    )
