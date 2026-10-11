#!/usr/bin/env python3
"""Forwarding shim: `runbook_gates.py etcd-restore`, under the path it had before #4344.

The etcd restore gates moved into `runbook_gates_lib/etcd_restore.py` behind
`scripts/deploy_tools/runbook_gates.py`. This path survives because
`ansible/roles/setup/k3s/defaults/main.yml` names it, and that is the bring-up k3s role,
which only a hand apply reaches. Retire the shim in the change that next edits that file.

Usage:
    uv run python scripts/deploy_tools/k3s_etcd_restore_gates.py [--gate N] <snapshot-name>
"""

import sys
from pathlib import Path as _Path

# Reach this directory's runbook_gates.py when the shim is run from anywhere.
sys.path.insert(0, str(_Path(__file__).resolve().parent))

import runbook_gates

if __name__ == "__main__":
    sys.exit(runbook_gates.main(["etcd-restore", *sys.argv[1:]]))
