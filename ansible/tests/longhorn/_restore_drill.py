"""The rendered-drill harness both restore-drill selection suites run against.

Extracted when the retry suite needed the same thing the operator-pin suite already built: the
drill rendered from its own defaults, with `k3s` and `logger` stubbed by bash functions exported
into the child. Bash resolves a function name before it searches PATH, which is why a stub on
PATH would lose to the real `/usr/local/bin/k3s` the script prepends.

The stub answers the drill's reads from fixtures and either refuses the `apply` — which stops a
selection test right after the attempt stamp is written — or plays the restore through to a
passing probe.
"""

import json
import os
import subprocess
from pathlib import Path

from jinja2 import Environment
from lib import yaml_fast

from _helpers import ANSIBLE

K3S = ANSIBLE / "roles" / "setup" / "k3s"
DRILL = K3S / "templates" / "longhorn-restore-drill.sh.j2"

STUB = r"""
fixtures="$K3S_STUB_FIXTURES"
printf '%s\n' "$*" >>"$fixtures/calls"
case "$*" in
  *"get backups.longhorn.io -o json"*) cat "$fixtures/backups.json" ;;
  *"get volume -o json"*) cat "$fixtures/volumes.json" ;;
  *"get backuptarget"*) printf 's3://bucket@region/' ;;
  *"apply -f"*) [[ "$K3S_STUB_RESTORE" == pass ]] || return 1 ;;
  *"get volume restore-drill-"*) printf 'detached false' ;;
  *"get pod "*) printf 'Succeeded' ;;
  *"logs "*) printf '%s\n' "${K3S_STUB_PROBE:-files=3 bytes=4096}" ;;
  *) : ;;
esac
"""


def volume(pvc: str, actual_size: int = 1024) -> dict:
    return {
        "metadata": {
            "name": f"pvc-{pvc}",
            "labels": {"recurring-job-group.longhorn.io/default": "enabled"},
        },
        "spec": {"size": "16777216", "backupBlockSize": "16777216"},
        "status": {
            "actualSize": actual_size,
            "kubernetesStatus": {"pvcName": pvc, "namespace": "homelab"},
        },
    }


def backup(pvc: str) -> dict:
    return {
        "metadata": {"name": f"backup-{pvc}"},
        "status": {
            "state": "Completed",
            "volumeName": f"pvc-{pvc}",
            "snapshotCreatedAt": "2026-09-20T00:00:00Z",
        },
    }


def render(stamp_dir: Path) -> str:
    defaults = yaml_fast.safe_load((K3S / "defaults" / "main.yml").read_text())
    defaults["k3s_longhorn_restore_drill_stamp_dir"] = str(stamp_dir)
    defaults["sys_user"] = "ubuntu"
    return Environment().from_string(DRILL.read_text()).render(**defaults)


def harness(
    tmp_path: Path,
    pvcs: list[str],
    backed: list[str] | None = None,
    actual_sizes: dict[str, int] | None = None,
):
    """A rendered drill plus a runner: `run(argv, env=..., restore=...)` -> CompletedProcess.

    `actual_sizes` overrides a volume's `status.actualSize`, which the empty-content waiver reads
    to decide whether a declared-empty volume is still empty.
    """
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    sizes = actual_sizes or {}
    (fixtures / "volumes.json").write_text(
        json.dumps({"items": [volume(p, sizes.get(p, 1024)) for p in pvcs]})
    )
    (fixtures / "backups.json").write_text(
        json.dumps({"items": [backup(p) for p in (pvcs if backed is None else backed)]})
    )
    stamp_dir = tmp_path / "stamps"
    (stamp_dir / "attempts").mkdir(parents=True)
    script = tmp_path / "drill.sh"
    script.write_text(render(stamp_dir))

    def run(
        argv: list[str] | None = None, env: dict | None = None, restore: str = "fail"
    ):
        child_env = {
            "PATH": os.environ["PATH"],
            "K3S_STUB_FIXTURES": str(fixtures),
            "K3S_STUB_RESTORE": restore,
            "BASH_FUNC_k3s%%": f"() {{ {STUB} }}",
            "BASH_FUNC_logger%%": "() { :; }",
            **(env or {}),
        }
        return subprocess.run(
            ["bash", str(script), *(argv or [])],
            env=child_env,
            capture_output=True,
            text=True,
        )

    run.stamp_dir = stamp_dir
    return run


def selected(run, proc: subprocess.CompletedProcess) -> str:
    """Which volume the drill tried to restore: the attempt stamp it refreshed before the apply."""
    assert proc.returncode == 1, proc.stderr
    assert "could not create the drill volume" in proc.stderr, proc.stderr
    attempts = run.stamp_dir / "attempts"
    return max(attempts.iterdir(), key=lambda p: p.stat().st_mtime).name
