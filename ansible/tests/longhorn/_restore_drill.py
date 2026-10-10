"""The rendered-drill harness both restore-drill selection suites run against.

The drill is rendered from its own defaults, with `k3s` and `logger` stubbed by bash functions
exported into the child. Bash resolves a function name before it searches PATH, which is why a stub on
PATH would lose to the real `/usr/local/bin/k3s` the script prepends.

The stub answers the drill's reads from fixtures and either refuses the `apply` — which stops a
selection test right after the attempt stamp is written — or plays the restore through to a
passing probe.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from lib.proc_testing import run as launch

from _shell_render import render_shell_script

DRILL_TEMPLATE = ("setup", "k3s", "longhorn-restore-drill.sh.j2")

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


def volume(pvc: str, actual_size: int = 1024, bound: bool = True) -> dict:
    """A tiered Volume CR named `pvc-<pvc>`; `bound=False` blanks its pvcName, as for a released PVC."""
    return {
        "metadata": {
            "name": f"pvc-{pvc}",
            "labels": {"recurring-job-group.longhorn.io/default": "enabled"},
        },
        "spec": {"size": "16777216", "backupBlockSize": "16777216"},
        "status": {
            "actualSize": actual_size,
            "kubernetesStatus": {
                "pvcName": pvc if bound else "",
                "namespace": "homelab",
            },
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
    """The drill as the host runs it, with its stamp directory pointed at `stamp_dir`.

    The render goes through `_shell_render`, so a value this harness executes cannot differ from
    what the shellcheck gate lints or what Ansible deploys. The override beats the role's own
    `k3s_longhorn_restore_drill_stamp_dir` — which is the point: left alone it would write the
    production stamp tree.
    """
    return render_shell_script(
        *DRILL_TEMPLATE,
        overrides={"k3s_longhorn_restore_drill_stamp_dir": str(stamp_dir)},
    )


@dataclass
class Harness:
    """`run(argv, env=..., restore=...)` -> CompletedProcess, plus the stamp tree it writes."""

    script: Path
    fixtures: Path
    stamp_dir: Path

    def __call__(
        self,
        argv: list[str] | None = None,
        env: dict | None = None,
        restore: str = "fail",
    ) -> subprocess.CompletedProcess:
        child_env = {
            "PATH": os.environ["PATH"],
            "K3S_STUB_FIXTURES": str(self.fixtures),
            "K3S_STUB_RESTORE": restore,
            "BASH_FUNC_k3s%%": f"() {{ {STUB} }}",
            "BASH_FUNC_logger%%": "() { :; }",
            **(env or {}),
        }
        return launch(["bash", str(self.script), *(argv or [])], env=child_env)


def harness(
    tmp_path: Path,
    pvcs: list[str],
    backed: list[str] | None = None,
    actual_sizes: dict[str, int] | None = None,
    no_pvc: set[str] | None = None,
) -> Harness:
    """A rendered drill plus a runner: `run(argv, env=..., restore=...)` -> CompletedProcess.

    `actual_sizes` overrides a volume's `status.actualSize`, which the empty-content waiver reads
    to decide whether a declared-empty volume is still empty. `no_pvc` names the volumes whose
    `kubernetesStatus.pvcName` is empty.
    """
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    sizes = actual_sizes or {}
    (fixtures / "volumes.json").write_text(
        json.dumps(
            {
                "items": [
                    volume(p, sizes.get(p, 1024), p not in (no_pvc or set()))
                    for p in pvcs
                ]
            }
        )
    )
    (fixtures / "backups.json").write_text(
        json.dumps({"items": [backup(p) for p in (pvcs if backed is None else backed)]})
    )
    stamp_dir = tmp_path / "stamps"
    (stamp_dir / "attempts").mkdir(parents=True)
    script = tmp_path / "drill.sh"
    script.write_text(render(stamp_dir))

    return Harness(script, fixtures, stamp_dir)


def selected(run, proc: subprocess.CompletedProcess) -> str:
    """Which volume the drill tried to restore: the attempt stamp it refreshed before the apply."""
    assert proc.returncode == 1, proc.stderr
    assert "could not create the drill volume" in proc.stderr, proc.stderr
    attempts = run.stamp_dir / "attempts"
    return max(attempts.iterdir(), key=lambda p: p.stat().st_mtime).name
