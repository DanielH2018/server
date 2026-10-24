"""The rendered-drill harness the restore-drill suites run against.

The drill is rendered from its own defaults, with `k3s` and `logger` stubbed by bash functions
exported into the child. Bash resolves a function name before it searches PATH, which is why a stub on
PATH would lose to the real `/usr/local/bin/k3s` the script prepends.

The stub answers the drill's reads from fixtures and either refuses the `apply` — which stops a
selection test right after the attempt stamp is written — or plays the restore through to a
passing probe.

The stamp ConfigMap is a fake API object, `cm.json` in the fixtures directory. `get configmap`
prints it, and `patch configmap` merges the patch's `data` into it, so state persists across runs
the way the cluster's does. A harness built with `cm=False` has no such file, which the stub
reports as NotFound (`--ignore-not-found`: rc 0, empty output). `K3S_STUB_GET_CM=fail` fails
the read, and `K3S_STUB_PATCH_FAILS=<text>` fails any patch whose call contains the text.
"""

import json
import os
import re
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
  *"get configmap"*)
    [[ "${K3S_STUB_GET_CM:-}" != fail ]] || return 1
    if [[ -f "$fixtures/cm.json" ]]; then cat "$fixtures/cm.json"; fi ;;
  *"patch configmap"*)
    [[ -f "$fixtures/cm.json" ]] || return 1
    [[ -z "${K3S_STUB_PATCH_FAILS:-}" || "$*" != *"$K3S_STUB_PATCH_FAILS"* ]] || return 1
    patch=""; prev=""
    for a in "$@"; do [[ "$prev" == -p ]] && patch="$a"; prev="$a"; done
    jq -c --argjson p "$patch" '.data = ((.data // {}) + $p.data)' "$fixtures/cm.json" \
      >"$fixtures/cm.tmp" && mv "$fixtures/cm.tmp" "$fixtures/cm.json" ;;
  *"get backups.longhorn.io -o json"*) cat "$fixtures/backups.json" ;;
  *"get volume -o json"*) cat "$fixtures/volumes.json" ;;
  *"get backuptarget"*) printf 's3://bucket@region/' ;;
  *"apply -f"*) [[ "$K3S_STUB_RESTORE" == pass ]] || return 1 ;;
  *"get volume restore-drill-"*) printf '%s' "${K3S_STUB_VOLSTATE:-detached false}" ;;
  *"get pod "*) printf 'Succeeded' ;;
  *"logs "*) printf '%s\n' "${K3S_STUB_PROBE:-files=3 bytes=4096}" ;;
  *) : ;;
esac
"""


def volume(
    pvc: str,
    actual_size: int = 1024,
    bound: bool = True,
    labels: list[str] | None = None,
) -> dict:
    """A tiered Volume CR named `pvc-<pvc>`; `bound=False` blanks its pvcName, as for a released PVC.

    `labels` replaces the `default` group label, each key set to `enabled`.
    """
    keys = labels or ["recurring-job-group.longhorn.io/default"]
    return {
        "metadata": {
            "name": f"pvc-{pvc}",
            "labels": dict.fromkeys(keys, "enabled"),
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
    """The drill as the host runs it, with its OLD stamp directory pointed at `stamp_dir`.

    Only the one-time import reads that directory now; the state is the ConfigMap. The render goes
    through `_shell_render`, so a value this harness executes cannot differ from what the
    shellcheck gate lints or what Ansible deploys. The override beats the role's own
    `k3s_longhorn_restore_drill_stamp_dir`, which is the point: left alone it would import the
    production stamp tree.
    """
    return render_shell_script(
        *DRILL_TEMPLATE,
        overrides={"k3s_longhorn_restore_drill_stamp_dir": str(stamp_dir)},
    )


@dataclass
class Harness:
    """`run(argv, env=..., restore=...)` -> CompletedProcess, plus the fake ConfigMap it writes.

    `stamp_dir` is the old host directory, which only the import arm reads. `cm` is the
    ConfigMap's `data` as the last `patch` left it, and `calls` is every `k3s` call the stub saw.
    """

    script: Path
    fixtures: Path
    stamp_dir: Path

    @property
    def cm(self) -> dict:
        return json.loads((self.fixtures / "cm.json").read_text())["data"]

    @property
    def calls(self) -> list[str]:
        path = self.fixtures / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def seed(self, keys: dict) -> None:
        """Set ConfigMap keys before a run, values stringified as the API holds them."""
        path = self.fixtures / "cm.json"
        doc = json.loads(path.read_text())
        doc["data"].update({k: str(v) for k, v in keys.items()})
        path.write_text(json.dumps(doc))

    def epoch(self, key: str) -> int:
        return int(self.cm[key])

    def child_env(self, restore: str = "fail", env: dict | None = None) -> dict:
        return {
            "PATH": os.environ["PATH"],
            "K3S_STUB_FIXTURES": str(self.fixtures),
            "K3S_STUB_RESTORE": restore,
            "BASH_FUNC_k3s%%": f"() {{ {STUB} }}",
            "BASH_FUNC_logger%%": "() { :; }",
            **(env or {}),
        }

    def __call__(
        self,
        argv: list[str] | None = None,
        env: dict | None = None,
        restore: str = "fail",
    ) -> subprocess.CompletedProcess:
        return launch(
            ["bash", str(self.script), *(argv or [])],
            env=self.child_env(restore, env),
        )


def harness(
    tmp_path: Path,
    pvcs: list[str],
    backed: list[str] | None = None,
    actual_sizes: dict[str, int] | None = None,
    no_pvc: set[str] | None = None,
    labels: dict[str, list[str]] | None = None,
    cm: bool = True,
    imported: bool = True,
) -> Harness:
    """A rendered drill plus a runner: `run(argv, env=..., restore=...)` -> CompletedProcess.

    `actual_sizes` overrides a volume's `status.actualSize`, which the empty-content waiver reads
    to decide whether a declared-empty volume is still empty. `no_pvc` names the volumes whose
    `kubernetesStatus.pvcName` is empty. `labels` gives a volume its label keys in place of
    the `default` group label. `cm=False` leaves the stamp ConfigMap absent, and
    `imported=False` leaves it without `imported-at`, so the first run imports the old stamp
    directory.
    """
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    sizes = actual_sizes or {}
    (fixtures / "volumes.json").write_text(
        json.dumps(
            {
                "items": [
                    volume(
                        p,
                        sizes.get(p, 1024),
                        p not in (no_pvc or set()),
                        (labels or {}).get(p),
                    )
                    for p in pvcs
                ]
            }
        )
    )
    (fixtures / "backups.json").write_text(
        json.dumps({"items": [backup(p) for p in (pvcs if backed is None else backed)]})
    )
    if cm:
        data = {} if not imported else {"imported-at": "1"}
        (fixtures / "cm.json").write_text(json.dumps({"data": data}))
    stamp_dir = tmp_path / "stamps"
    stamp_dir.mkdir()
    script = tmp_path / "drill.sh"
    script.write_text(render(stamp_dir))

    return Harness(script, fixtures, stamp_dir)


def selected(run: Harness, proc: subprocess.CompletedProcess) -> str:
    """Which volume the drill tried to restore: the last attempt key it patched before the apply."""
    assert proc.returncode == 1, proc.stderr
    assert "could not create the drill volume" in proc.stderr, proc.stderr
    stamped = [
        m.group(1)
        for call in run.calls
        if "patch configmap" in call
        for m in re.finditer(r'"attempts\.([^"]+)"', call)
    ]
    assert stamped, run.calls
    return stamped[-1]
