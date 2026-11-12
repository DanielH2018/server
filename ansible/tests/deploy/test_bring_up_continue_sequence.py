"""Guards on `ansible/bring-up.sh --continue` — the §8 hand-off to Ansible.

Two facts about that sequence are load-bearing and neither is visible from a green run.

THE CLUSTER STEP. `ansible/README.md` §8 lists `k3s-bringup.yml` after `initial_setup.yml`
for a cluster node. A `--continue` that skips it leaves a rebuilt control-plane node with no
cluster for Flux to apply manifests to.

THE SCRIPT STEPS. A `./scripts/...` command the branch runs must exist; the branch once kept
calling `scripts/deploy.sh` after the script was deleted, so its last step could only fail.

Run: uv run pytest ansible/tests/deploy/test_bring_up_continue_sequence.py
"""

import re

from _helpers import ANSIBLE

SCRIPT = (ANSIBLE / "bring-up.sh").read_text()

# The `--continue` body, from the `if` to the here-doc it ends with. Read as a slice rather
# than as the whole file so a command mentioned in the header comment cannot satisfy a test
# about what the branch RUNS.
_CONTINUE = re.search(
    r'if \[\[ "\$CONTINUE" == true \]\]; then(.*?)\n  exit 0', SCRIPT, re.S
)


def _steps(body: str | None = None) -> list[tuple[str, str]]:
    """Each playbook or `./scripts/` command the `--continue` branch runs, in order."""
    if body is None:
        assert _CONTINUE, (
            "the --continue branch is no longer recognisable in bring-up.sh"
        )
        body = _CONTINUE.group(1)
    # Anchored at the start of a line: the else arm ECHOES the agent-join command, and a
    # command inside a string is printed advice, not a step this branch runs.
    return re.findall(
        r"^\s*(?:uv run ansible-playbook ansible/([\w-]+)\.yml|(\./scripts/[\w/.-]+))",
        body,
        re.M,
    )


def _missing_scripts(body: str | None = None) -> list[str]:
    scripts = [script for _, script in _steps(body) if script]
    return [s for s in scripts if not (ANSIBLE.parent / s).is_file()]


def test_k3s_bringup_runs_after_initial_setup():
    order = [playbook or script for playbook, script in _steps()]
    assert order == ["preflight", "initial_setup", "k3s-bringup"], (
        f"--continue runs {order}"
    )


def test_every_script_the_branch_runs_exists():
    assert not _missing_scripts(), "--continue runs scripts that do not exist"


def test_a_step_naming_a_deleted_script_is_flagged():
    body = '  uv run ansible-playbook ansible/preflight.yml\n  ./scripts/deploy.sh -e target="$HOST"\n'
    assert _missing_scripts(body) == ["./scripts/deploy.sh"]


def test_the_cluster_step_is_gated_on_k3s_server_hosts():
    """Ungated it would fail k3s-bringup.yml's own assert on every non-server host."""
    assert _CONTINUE, "the --continue branch is no longer recognisable in bring-up.sh"
    assert "k3s_server_hosts" in _CONTINUE.group(1)


def test_the_branch_runs_no_deploy_playbook():
    """deploy.yml runs no role on any host; Flux and the host packages apply every workload."""
    assert "ansible/deploy.yml" not in SCRIPT
