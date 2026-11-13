"""A k3s node's read-only kubeconfig oneshot, run from its rendered host package.

Both k3s components ship the same script, so daniel-box's and daniel-server's package tests run
it through this one helper.
"""

import os
import subprocess
from pathlib import Path

from lib import yaml_fast
from lib.proc_testing import run, write_exec

SCRIPT = "/usr/lib/homelab/k3s-readonly-kubeconfig"
UNIT = "/usr/lib/systemd/system/homelab-k3s-kubeconfig.service"
TOKEN = "the-token"


def run_kubeconfig(
    package, root: Path, ca_source: str, ca_text: str
) -> subprocess.CompletedProcess[str]:
    """Run the package's kubeconfig oneshot under `root`, with `ca_text` at `ca_source` there."""
    body = package.files[SCRIPT][0]
    script = write_exec(
        root / "k3s-readonly-kubeconfig",
        body.decode() if isinstance(body, bytes) else body,
    )
    source = root / ca_source.lstrip("/")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(ca_text)
    creds = root / "creds"
    creds.mkdir(exist_ok=True)
    (creds / "k3s_readonly_token").write_text(TOKEN)
    return run(
        [str(script)],
        env={
            **os.environ,
            "HOMELAB_ROOT": str(root),
            "KUBECONFIG_OWNER": os.environ.get("USER") or "ubuntu",
            "CREDENTIALS_DIRECTORY": str(creds),
        },
        check=False,
    )


def written_kubeconfig(root: Path) -> tuple[Path, dict]:
    """The kubeconfig the oneshot wrote under `root`, and its parsed content."""
    dest = next(root.glob("home/*/.kube/config"))
    return dest, yaml_fast.safe_load(dest.read_text())
