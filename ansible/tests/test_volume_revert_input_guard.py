"""Behaviour anchor for k8s/volume-revert's input guard — the first task the rollback path runs.

WHY THIS EXISTS. The guard's SHA clause was written as a bare filter::

    - volume_revert_sha | default('') | regex_search('^[0-9a-f]{8,}$')

`regex_search` returns the MATCHED STRING or None, and ansible-core 2.21 refuses a conditional
whose result is not a boolean:

    Conditional result (True) was derived from value of type 'str' ...
    Conditionals must have a boolean result.

So the assert aborted on every invocation, with a VALID sha, before the role touched anything.
k8s/volume-revert is the rollback half of the k8s auto-deploy design — it could never have run.
The task-6 drill hit this on the first real call, 2026-08-21.

Every existing test of this guard asserted its source text and passed throughout. That is the
gap this module closes: it runs the role and lets Ansible judge the conditional.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_K3S_STUB = """#!/bin/sh
case "$*" in
  *"app=longhorn-manager"*)
      printf '10.255.255.1' ;;
  *"get pvc"*)
      printf 'pvc-drilltest' ;;
  *"get snapshots.longhorn.io"*)
      printf '2026-08-21T21:00:00Z|false|autodeploy-drillsvc-cc101c62-drillsvc-config-20260821210000\\n' ;;
  *)
      : ;;
esac
exit 0
"""

_FAKE_BECOME = """#!/bin/sh
last=""
prev=""
for a in "$@"; do prev="$last"; last="$a"; done
if [ "$prev" = "-c" ]; then exec /bin/sh -c "$last"; fi
exec "$@"
"""

# `k8s_no_mutate: true` so the role runs its guard and its reads but nothing that would attach,
# revert or scale. That is exactly the surface under test: the guard is the first task, and it
# is not gated on k8s_no_mutate — a broken conditional there fails a dry run too.
_PLAY = """- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    k8s_namespace: homelab
    k8s_no_mutate: true
    ansible_hostname: testnode
    ansible_become_exe: "{become_exe}"
  tasks:
    - name: Revert the volume
      ansible.builtin.include_role:
        name: k8s/volume-revert
      vars:
        volume_revert_service: drillsvc
        volume_revert_claims: {claims}
        volume_revert_sha: "{sha}"
    - name: Prove the guard let us through
      ansible.builtin.debug:
        msg: "GUARD_PASSED"
"""


def _run_revert_guard(
    sha: str, claims: str = "[drillsvc-config]"
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        k3s_stub = bin_dir / "k3s"
        k3s_stub.write_text(_K3S_STUB)
        k3s_stub.chmod(0o755)

        fake_become = bin_dir / "fake_become"
        fake_become.write_text(_FAKE_BECOME)
        fake_become.chmod(0o755)

        playbook = tmp_path / "play.yml"
        playbook.write_text(
            _PLAY.format(become_exe=fake_become, sha=sha, claims=claims)
        )

        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["ANSIBLE_LOG_PATH"] = str(tmp_path / "ansible.log")
        env["ANSIBLE_NOCOLOR"] = "1"

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
def test_volume_revert_input_guard_accepts_a_real_sha() -> None:
    """A valid eight-hex-digit deploy tag must pass the guard.

    The assertion on the message text is the load-bearing one: a bare `regex_search` clause
    fails here with "Conditionals must have a boolean result", and naming that string is what
    makes the regression legible rather than just a non-zero exit.
    """
    result = _run_revert_guard("cc101c62")
    assert "Conditionals must have a boolean result" not in result.stdout, (
        "k8s/volume-revert's input guard rejected a VALID sha because its conditional returns a "
        "string rather than a boolean. Add `is not none` to the regex_search clause.\n"
        f"STDOUT:\n{result.stdout}"
    )
    assert "GUARD_PASSED" in result.stdout, (
        f"the role must run to completion under k8s_no_mutate.\nSTDOUT:\n{result.stdout}"
    )


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
def test_volume_revert_input_guard_still_rejects_a_bad_sha() -> None:
    """The control for the test above. `is not none` must not defang the guard: a SHA that is
    not eight-or-more lowercase hex digits still has to stop the play before the scale-down."""
    result = _run_revert_guard("nope")
    assert result.returncode != 0
    assert "GUARD_PASSED" not in result.stdout
    assert "volume_revert_sha of eight or more lowercase hex digits" in result.stdout


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)
def test_volume_revert_input_guard_rejects_a_mapping() -> None:
    """Jinja's `sequence` test is satisfied by a dict — it only checks for `__getitem__` and the
    absence of `strip` — so a mapping clears both `is sequence` and `is not string` and would
    otherwise reach `loop:` in main.yml's per-claim include. `is not mapping` closes that."""
    result = _run_revert_guard("cc101c62", claims="{drillsvc-config: 1}")
    assert result.returncode != 0
    assert "GUARD_PASSED" not in result.stdout
    assert "volume_revert_claims LIST" in result.stdout
