"""Every apply the deployer prints runs through the repo's pinned env (#4413).

A bare `ansible-playbook` resolves to the uv-tool shim, which lacks the `community.docker`
deps, so the command an operator pastes from land.sh's `needs-manual-apply` note, the journal
or the `manual_plane` Discord alert fails. `gitops_markers.PLAYBOOK_CMD` is the one prefix
every surface prints.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_pinned_env.py
"""

import re
import subprocess

from lib.repo_paths import REPO

from deployer_lib.deploy_alert_text import (
    manual_plane_alert,
    secrets_deferred_alert,
    tasks_deferred_alert,
)
from deployer_lib.deploy_remediation import (
    broad_remediation,
    k8s_remediation,
    manual_plane_remediation,
)

PINNED = "uv run ansible-playbook"

# An `ansible-playbook` that the pinned prefix does not lead.
_BARE = re.compile(r"(?<!uv run )ansible-playbook\b")


def _bare(text: str) -> list[str]:
    return [text[max(0, m.start() - 20) : m.end() + 30] for m in _BARE.finditer(text)]


def test_no_surface_composes_a_bare_playbook_command():
    """The Verify-by of #4413, over the tracked deployer_lib and monitor-bridge's page."""
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "--",
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/*.py",
            "ansible/roles/k8s/monitor-bridge/files/checks/gitops.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    assert "ansible/roles/k8s/monitor-bridge/files/checks/gitops.py" in listed
    assert (
        "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py"
        in listed
    )
    bare = re.compile(r"(?<!uv run )ansible-playbook \{")
    offenders = [
        f"{rel}:{n}: {line.strip()}"
        for rel in listed
        for n, line in enumerate((REPO / rel).read_text().splitlines(), 1)
        if bare.search(line)
    ]
    assert offenders == []


def test_the_manual_plane_apply_runs_through_the_pinned_env():
    """land.sh's note and the journal line quote this composer."""
    cmd = manual_plane_remediation({"k3s"}, {"k3s": frozenset({"kubeconfig"})})
    assert f"`{PINNED} ansible/k3s-bringup.yml --tags kubeconfig`" in cmd
    assert _bare(cmd) == []


def test_a_role_gated_off_the_tick_host_keeps_its_target_behind_the_pinned_prefix():
    cmd = manual_plane_remediation({"optimize_pi"})
    assert (
        f"`{PINNED} ansible/initial_setup.yml --tags optimize_pi -e target=daniel-pi`"
        in cmd
    )
    assert _bare(cmd) == []


def test_the_manual_plane_discord_alert_runs_through_the_pinned_env():
    msg = manual_plane_alert(
        "abc123def4567890", manual_plane_remediation({"k3s"}), "owed.jsonl"
    )
    assert f"`{PINNED} ansible/k3s-bringup.yml --tags k3s`" in msg
    assert _bare(msg) == []


def test_every_broad_remediation_form_runs_through_the_pinned_env():
    """The whole-playbook, placeholder and deploy-plane forms the same module prints."""
    forms = {
        "deploy plane": broad_remediation(True, False),
        "placeholder": broad_remediation(False, True, set()),
        "bring-up playbook": broad_remediation(
            False, True, set(), playbooks={"ansible/bootstrap.yml"}
        ),
    }
    assert f"`{PINNED} ansible/deploy.yml`" in forms["deploy plane"]
    assert f"`{PINNED} ansible/initial_setup.yml --tags <role>`" in forms["placeholder"]
    assert f"`{PINNED} ansible/bootstrap.yml`" in forms["bring-up playbook"]
    assert {name: _bare(text) for name, text in forms.items() if _bare(text)} == {}


def test_the_k8s_redeploy_runs_through_the_pinned_env():
    scoped = k8s_remediation({"sonarr"}, {"sonarr"})
    full = k8s_remediation({"manifests", "sonarr"}, {"sonarr"})
    assert f"`{PINNED} ansible/deploy.yml --tags sonarr`" in scoped
    assert f"`{PINNED} ansible/deploy.yml`" in full
    assert _bare(scoped) == [] and _bare(full) == []


def test_the_deferred_redeploy_alerts_run_through_the_pinned_env():
    alerts = [
        secrets_deferred_alert("abc123def4567890"),
        tasks_deferred_alert("abc123def4567890", {"sonarr"}),
    ]
    assert all(f"`{PINNED} ansible/deploy.yml --tags <svc>`" in a for a in alerts)
    assert [_bare(a) for a in alerts] == [[], []]
    assert f"`{PINNED} ansible/initial_setup.yml --tags <role>`" in alerts[0]
