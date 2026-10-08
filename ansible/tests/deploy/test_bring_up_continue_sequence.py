"""Guards on `ansible/bring-up.sh --continue` — the §8 hand-off to Ansible.

Two facts about that sequence are load-bearing and neither is visible from a green run.

THE CLUSTER STEP. `ansible/README.md` §8 lists `k3s-bringup.yml` between `initial_setup.yml`
and the deploy for a cluster node. A `--continue` that skips it
reaches the deploy on a rebuilt control-plane node with no cluster to apply manifests to.

THE DEPLOY STEP. A bare `ansible-playbook deploy.yml` takes none of the locks every
holder in docs/deploying.md's *Who holds the tree lock* takes on this tree, so a bring-up that overlaps
either interleaves two writers. `scripts/deploy.sh` is the entry point that holds them.

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


def _steps() -> list[str]:
    """Each playbook or deploy command the `--continue` branch runs, in order."""
    assert _CONTINUE, "the --continue branch is no longer recognisable in bring-up.sh"
    # Anchored at the start of a line: the else arm ECHOES the agent-join command, and a
    # command inside a string is printed advice, not a step this branch runs.
    return re.findall(
        r"^\s*(?:uv run ansible-playbook ansible/([\w-]+)\.yml|(\./scripts/deploy\.sh))",
        _CONTINUE.group(1),
        re.M,
    )


def test_k3s_bringup_runs_between_initial_setup_and_the_deploy():
    order = [playbook or deploy for playbook, deploy in _steps()]
    assert order == [
        "preflight",
        "initial_setup",
        "k3s-bringup",
        "./scripts/deploy.sh",
    ], f"--continue runs {order}"


def test_the_cluster_step_is_gated_on_k3s_server_hosts():
    """Ungated it would fail k3s-bringup.yml's own assert on every non-server host."""
    assert _CONTINUE, "the --continue branch is no longer recognisable in bring-up.sh"
    assert "k3s_server_hosts" in _CONTINUE.group(1)


def test_the_deploy_step_does_not_bypass_the_locks():
    """`ansible-playbook deploy.yml` here is the bypass this guard exists to refuse."""
    assert "ansible-playbook ansible/deploy.yml" not in SCRIPT
