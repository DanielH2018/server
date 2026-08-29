"""The staging gate's checkout and lock paths exist twice, and only one copy runs.

`roles/setup/hypervisor/defaults/main.yml` is where they are DECLARED — install.yml clones the
checkout there and teardown.yml removes it. `scripts/deploy_tools/staging_gate_remote.sh` is
where they are USED, as bash literals, because that script is piped over ssh by staging_gate.py
and never rendered by Ansible. It cannot read a Jinja var, so the duplication is structural
rather than sloppy.

The failure it invites is silent in the worst direction. Move the checkout in the role and the
gate keeps cding to the old path: `cd` fails, every tick answers PREP_FAILED, and PREP_FAILED
maps to NO_VERDICT — which the deployer reports as "staging could not be asked, which is not a
rejection" and then deploys prod anyway. A gate that has stopped working looks exactly like a
staging host that is down.

Same shape as the timeout fallbacks pinned in
gitops_deploy/files/test_gitops_discord_contract.py: when a value has to exist in two places,
the test is what makes them one value.
"""

import pathlib
import re

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
_DEFAULTS = (
    _REPO / "ansible" / "roles" / "setup" / "hypervisor" / "defaults" / "main.yml"
)
_REMOTE = _REPO / "scripts" / "deploy_tools" / "staging_gate_remote.sh"


def shell_assignments(source: str) -> dict[str, str]:
    """Bare `NAME=/literal/path` assignments in a shell script.

    Deliberately does not match `NAME="$OTHER"` or anything interpolated: the point is to read
    the literal the script will actually cd to, not to re-implement shell.
    """
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(/[^\s\"'$]*)$", source, re.MULTILINE))


def test_the_remote_script_uses_the_paths_the_role_provisions():
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    literals = shell_assignments(_REMOTE.read_text())
    for shell_name, ansible_name in (
        ("REPO", "hypervisor_staging_gate_repo"),
        ("LOCK", "hypervisor_staging_gate_lock"),
    ):
        assert shell_name in literals, (
            f"{_REMOTE.name} no longer assigns {shell_name} to a literal path — if it became "
            f"a variable, this guard stopped guarding anything."
        )
        assert literals[shell_name] == defaults[ansible_name], (
            f"{_REMOTE.name} uses {shell_name}={literals[shell_name]} while "
            f"{ansible_name} provisions {defaults[ansible_name]}. The script is piped over ssh "
            f"and cannot read the Ansible var, so the mismatch makes every tick answer "
            f"PREP_FAILED -> NO_VERDICT, which reads as a staging outage rather than a bug."
        )


def test_a_drifted_path_is_caught():
    # Red proof, on the same parser. The rejected input is the drift this guard exists for:
    # the role moved the checkout and the script kept the old literal.
    assert shell_assignments("REPO=/home/ubuntu/server-staging\n") == {
        "REPO": "/home/ubuntu/server-staging"
    }
    stale = shell_assignments("REPO=/home/ubuntu/server\n")
    assert stale["REPO"] != "/home/ubuntu/server-staging", (
        "the parser must read the literal, or a drifted path reads as agreeing with whatever "
        "the role declares"
    )
    # An interpolated assignment is not a literal and must not be mistaken for one.
    assert shell_assignments('REPO="$SOMETHING"\n') == {}


def test_the_gate_does_not_deploy_from_the_hosts_own_checkout():
    # The finding itself, stated as a property rather than as a path comparison: whatever the
    # two files agree on, it must not be daniel-server's production checkout. Agreeing on the
    # wrong value is exactly how M-2 read green for as long as it did.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["hypervisor_staging_gate_repo"] != "/home/ubuntu/server", (
        "the staging gate must not render from the host's own checkout — it fast-forwards the "
        "tree it deploys from to the SHA under test (2026-08-29 review M-2)"
    )
