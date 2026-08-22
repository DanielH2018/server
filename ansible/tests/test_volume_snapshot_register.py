"""Behaviour anchor for k8s/volume-snapshot's ATTACHED-volume path — the path every one of the
thirteen roles declaring `k8s_autodeploy_snapshot_pvcs` takes on a normal deploy.

WHY THIS EXISTS. Slice 7b's task 5 added a second readiness wait (the retake, for a volume it
had just attached in maintenance mode) and deliberately registered it to `volume_snapshot_ready`
— the same name the first wait uses — so that the downstream fail task would "keep working
unmodified against whichever attempt actually produced a result".

That reasoning is wrong about Ansible, and every existing unit test agreed with it because none
of them ran the role. **A SKIPPED task still sets the variable it registers to**, to a result
carrying `skipped: true` and no `stdout` key at all. On the attached path the retake wait is
skipped, so it overwrote the first wait's genuine `true|false` with a stdout-less skip result,
and `volume_snapshot_ready.stdout | default('')` downstream rendered `''`. The deploy then
failed at "Fail on a snapshot that never became usable" over a snapshot that was healthy and
`readyToUse` — measured 2026-08-21 on speedtest-config during the task-6 drill.

So this test runs the real role end to end against a stubbed `k3s kubectl` and asserts the
attached path completes. A rendered-expression test cannot catch this class: the bug is in when
Ansible assigns a register, not in any expression's text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The stub stands in for `k3s kubectl`. It answers each call this role makes, and it learns the
# snapshot's name from the `apply` it receives on stdin rather than guessing it — the name
# carries a per-run token computed inside the role, so no fixture can know it up front. The
# prune's "found this run's own snapshot" assert then sees the same name in the listing.
_K3S_STUB = """#!/bin/sh
set -e
case "$*" in
  *"get pvc"*)
      printf 'pvc-drilltest' ;;
  *"apply -f -"*)
      name=$(sed -n 's/^  name: //p')
      printf '%s' "$name" > "$STUB_STATE/name"
      printf 'snapshot.longhorn.io/%s created\\n' "$name" ;;
  *"get snapshots.longhorn.io -o"*)
      printf '2026-08-21T21:00:00Z|false|%s\\n' "$(cat "$STUB_STATE/name")" ;;
  *"get snapshots.longhorn.io"*)
      printf 'true|false' ;;
  *"get volumes.longhorn.io"*)
      printf 'attached' ;;
  *)
      : ;;
esac
exit 0
"""

# A `sudo`/`become_exe` passthrough, copied from test_longhorn_api.py: finds the trailing
# `-c '<command>'` the sudo become plugin builds and execs it as the current user. Nothing the
# role runs here needs real root, and the sandbox has no passwordless sudo to use.
_FAKE_BECOME = """#!/bin/sh
last=""
prev=""
for a in "$@"; do prev="$last"; last="$a"; done
if [ "$prev" = "-c" ]; then exec /bin/sh -c "$last"; fi
exec "$@"
"""

_PLAY = """- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    k8s_namespace: homelab
    k8s_no_mutate: false
    ansible_become_exe: "{become_exe}"
  tasks:
    - name: Snapshot the volume
      ansible.builtin.include_role:
        name: k8s/volume-snapshot
      vars:
        volume_snapshot_service: drillsvc
        volume_snapshot_claims: [drillsvc-config]
    - name: Prove the attached path completed
      ansible.builtin.debug:
        msg: "REACHED_END ready_out='{{{{ volume_snapshot_ready_out | default('undef') }}}}'"
"""


def _run_attached_path() -> subprocess.CompletedProcess[str]:
    """Run k8s/volume-snapshot for real on the attached-volume path, against a stubbed cluster."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        k3s_stub = bin_dir / "k3s"
        k3s_stub.write_text(_K3S_STUB)
        k3s_stub.chmod(0o755)

        fake_become = bin_dir / "fake_become"
        fake_become.write_text(_FAKE_BECOME)
        fake_become.chmod(0o755)

        # The role resolves its deploy tag with `git rev-parse --short=8 HEAD` and `chdir:
        # {{ playbook_dir }}/..`, so the playbook goes one level down and its parent gets a real
        # repository. Scrubbing GIT_* from the environment is not optional: `chdir` does not
        # override GIT_DIR, and a stray one would point this `rev-parse` — and anything else the
        # play ran — at the real repository instead of this throwaway.
        play_dir = tmp_path / "play"
        play_dir.mkdir()
        playbook = play_dir / "play.yml"
        playbook.write_text(_PLAY.format(become_exe=fake_become))

        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["STUB_STATE"] = str(state_dir)
        env["ANSIBLE_LOG_PATH"] = str(tmp_path / "ansible.log")
        env["ANSIBLE_NOCOLOR"] = "1"
        # Pin the interpreter rather than discovering it: the fact cache is keyed on `localhost`
        # and shared by every worktree, so a pruned tree's .venv fails this play with rc 127.
        env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable

        git_env = dict(env)
        git_env["GIT_AUTHOR_NAME"] = "drill"
        git_env["GIT_AUTHOR_EMAIL"] = "drill@example.invalid"
        git_env["GIT_COMMITTER_NAME"] = "drill"
        git_env["GIT_COMMITTER_EMAIL"] = "drill@example.invalid"
        for args in (
            ["git", "init", "-q"],
            ["git", "commit", "-q", "--allow-empty", "-m", "drill"],
        ):
            subprocess.run(
                args, cwd=tmp_path, env=git_env, check=True, capture_output=True
            )

        return subprocess.run(
            ["ansible-playbook", str(playbook), "-i", "localhost,"],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_skipped_retake_wait_keeps_first_read() -> None:
    """The attached-volume path must complete when the snapshot reports readyToUse.

    Before the fix this went red: the skipped retake wait clobbered `volume_snapshot_ready`, and
    the role failed with "did not report readyToUse" against a snapshot the stub reported as
    `true|false`. The assertion on the message is what names the regression if it returns — a
    bare returncode check would not say which failure came back.
    """
    result = _run_attached_path()
    assert "did not report readyToUse" not in result.stdout, (
        "k8s/volume-snapshot failed the attached-volume path on a snapshot that IS ready. The "
        "skipped retake wait has clobbered the first wait's register again — see this module's "
        f"docstring.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"the attached-volume path must complete.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    # The folded fact must carry the FIRST wait's reading, not a skip result's empty string.
    assert "REACHED_END ready_out='true|false'" in result.stdout, (
        "volume_snapshot_ready_out must hold the attached path's own readiness read.\n"
        f"STDOUT:\n{result.stdout}"
    )
