"""The rendered-drill harness the restore-drill suites run against.

The drill is rendered from the `longhorn-restore-drill` role's own defaults, with `kubectl`
stubbed by a bash function exported into the child. Bash resolves a function name before it
searches PATH, so the stub wins over any real `kubectl` on the host.

The stub answers the drill's reads from fixtures and either refuses the `apply` — which stops a
selection test right after the attempt stamp is written — or plays the restore through to a
passing probe. Like the real `kubectl apply -f`, it refuses a file that does not exist or is
empty, which is what an unset `MANIFEST` looks like.

`launch_in_image` runs the same script, stub and fixtures inside the pinned runner image with
`docker run --rm`. The image is BusyBox over musl for `mktemp`, `date`, `sort` and `stat`, so a
GNU-only flag passes on the host and fails there.

The stamp ConfigMap is a fake API object, `cm.json` in the fixtures directory. `get configmap`
prints it, and `patch configmap` merges the patch's `data` into it, so state persists across runs
the way the cluster's does. A harness built with `cm=False` has no such file, which the stub
reports as NotFound (`--ignore-not-found`: rc 0, empty output). `K3S_STUB_GET_CM=fail` fails
the read, and `K3S_STUB_PATCH_FAILS=<text>` fails any patch whose call contains the text.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from _helpers import load_yaml
from lib.proc_testing import run as launch
from lib.repo_paths import K8S_ROLES

from _shell_render import render_shell_script

DRILL_TEMPLATE = ("k8s", "longhorn-restore-drill", "longhorn-restore-drill.sh.j2")
DRILL_DEFAULTS = K8S_ROLES / "longhorn-restore-drill" / "defaults" / "main.yml"

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
  *"apply -f"*)
    manifest=""; prev=""
    for a in "$@"; do [[ "$prev" == -f ]] && manifest="$a"; prev="$a"; done
    [[ -s "$manifest" ]] || return 1
    [[ "$K3S_STUB_RESTORE" == pass ]] || return 1 ;;
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


def render() -> str:
    """The drill as the CronJob runs it.

    The render goes through `_shell_render`, so a value this harness executes cannot differ from
    what the shellcheck gate lints or what the script ConfigMap embeds.
    """
    return render_shell_script(*DRILL_TEMPLATE)


@dataclass
class Harness:
    """`run(argv, env=..., restore=...)` -> CompletedProcess, plus the fake ConfigMap it writes.

    `cm` is the ConfigMap's `data` as the last `patch` left it, and `calls` is every `kubectl`
    call the stub saw.
    """

    script: Path
    fixtures: Path

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
            "BASH_FUNC_kubectl%%": f"() {{ {STUB} }}",
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


def image_leg_available() -> bool:
    """Whether `docker run` can work here: the daemon answers, not merely a binary on PATH."""
    if not shutil.which("docker"):
        return False
    return launch(["docker", "info"], env=dict(os.environ)).returncode == 0


def runner_image() -> str:
    return load_yaml(DRILL_DEFAULTS)["longhorn_restore_drill_runner_image"]


def launch_in_image(
    run: "Harness", restore: str = "fail", env: dict | None = None
) -> subprocess.CompletedProcess:
    """`run`'s script, stub and fixtures inside the pinned runner image.

    The host `PATH` is not forwarded, so the image's own tools run. The working tree is mounted
    at its host path, read-write and as the caller's uid, so the stub's writes into the fixtures
    directory are removable by pytest afterwards. The stub function travels as an exported
    `BASH_FUNC_kubectl%%` variable, as on the host.
    """
    root = run.script.parent
    child = run.child_env(restore, env)
    args = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
    args += ["-v", f"{root}:{root}", "-e", "HOME=/tmp"]
    for key, value in child.items():
        if key != "PATH":
            args += ["-e", f"{key}={value}"]
    return launch(
        [*args, runner_image(), "bash", str(run.script)], env=dict(os.environ)
    )


def harness(
    tmp_path: Path,
    pvcs: list[str],
    backed: list[str] | None = None,
    actual_sizes: dict[str, int] | None = None,
    no_pvc: set[str] | None = None,
    labels: dict[str, list[str]] | None = None,
    cm: bool = True,
) -> Harness:
    """A rendered drill plus a runner: `run(argv, env=..., restore=...)` -> CompletedProcess.

    `actual_sizes` overrides a volume's `status.actualSize`, which the empty-content waiver reads
    to decide whether a declared-empty volume is still empty. `no_pvc` names the volumes whose
    `kubernetesStatus.pvcName` is empty. `labels` gives a volume its label keys in place of
    the `default` group label. `cm=False` leaves the stamp ConfigMap absent.
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
        (fixtures / "cm.json").write_text(json.dumps({"data": {}}))
    script = tmp_path / "drill.sh"
    script.write_text(render())

    return Harness(script, fixtures)


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
