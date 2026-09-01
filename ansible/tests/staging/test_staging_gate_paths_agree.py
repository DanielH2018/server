"""The staging gate's checkout and lock paths exist twice, and only one copy runs.

`roles/setup/hypervisor/defaults/main.yml` is where they are DECLARED — install.yml clones the
checkout there and teardown.yml removes it. `scripts/deploy_tools/staging_gate_remote.sh` is
where they are USED, as bash literals. That script is copied to the host verbatim rather than
templated, so it cannot read a Jinja var and the duplication is structural rather than sloppy.

The restricted key's dispatcher (roles/setup/hypervisor/templates/staging-gate-dispatch.sh.j2)
is NOT a third copy and must not become one: it is rendered by Ansible, so it reads
`hypervisor_staging_gate_repo` directly and cannot drift by construction. If that literal copy
ever retires, this guard retires with it rather than growing another literal to pin.

The failure it invites is silent in the worst direction. Move the checkout in the role and the
gate keeps cding to the old path: `cd` fails, every tick answers PREP_FAILED, and PREP_FAILED
maps to NO_VERDICT — which the deployer reports as "staging could not be asked, which is not a
rejection" and then deploys prod anyway. A gate that has stopped working looks exactly like a
staging host that is down.

Same shape as the timeout fallbacks pinned in
gitops_deploy/files/test_gitops_deploy_staging_timeouts.py: when a value has to exist in two places,
the test is what makes them one value.
"""

import re

import yaml
from _helpers import REPO

_REPO = REPO
_DEFAULTS = (
    _REPO / "ansible" / "roles" / "setup" / "hypervisor" / "defaults" / "main.yml"
)
_REMOTE = _REPO / "scripts" / "deploy_tools" / "staging_gate_remote.sh"
_ROLE = _REPO / "ansible" / "roles" / "setup" / "hypervisor"
_DISPATCH = _ROLE / "templates" / "staging-gate-dispatch.sh.j2"
_INSTALL = _ROLE / "tasks" / "install.yml"


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
            f"{ansible_name} provisions {defaults[ansible_name]}. The script is copied verbatim "
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


def test_the_dispatcher_execs_the_installed_runner_not_the_checkouts_copy():
    """The gate's mechanism must not be chosen by the commit it is judging.

    Until 2026-08-30 the dispatcher exec'd `./scripts/deploy_tools/staging_gate_remote.sh` from
    the tree under test. Two consequences: a commit could gate itself with its own edited copy
    of the gate, and rewinding that checkout removed the runner outright — eleven backfill runs
    came back 127 and read as an authentication failure.
    """
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    # The exec that hands over the request, not the `exec 0</dev/null` that closes stdin.
    exec_line = next(
        line
        for line in _DISPATCH.read_text().splitlines()
        if line.strip().startswith("exec ") and "$SHA" in line
    )
    assert "hypervisor_staging_gate_runner_path" in exec_line, (
        f"the dispatcher execs `{exec_line.strip()}` rather than the installed runner. Anything "
        f"resolved inside the checkout is code the SHA under test controls."
    )
    assert (
        defaults["hypervisor_staging_gate_runner_path"]
        != defaults["hypervisor_staging_gate_repo"]
        and "hypervisor_staging_gate_runner_path" in _INSTALL.read_text()
    ), "install.yml must place the runner at that path, or the dispatcher execs nothing"


def test_a_runner_exec_inside_the_checkout_is_caught():
    # Red proof: the shape this guard rejects is the shape that shipped.
    stale = '  exec ./scripts/deploy_tools/staging_gate_remote.sh "$SHA" "$TAGS"'
    assert "hypervisor_staging_gate_runner_path" not in stale


def test_the_gate_does_not_deploy_from_the_hosts_own_checkout():
    # The finding itself, stated as a property rather than as a path comparison: whatever the
    # two files agree on, it must not be daniel-server's production checkout. Agreeing on the
    # wrong value is exactly how M-2 read green for as long as it did.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["hypervisor_staging_gate_repo"] != "/home/ubuntu/server", (
        "the staging gate must not render from the host's own checkout — it fast-forwards the "
        "tree it deploys from to the SHA under test (2026-08-29 review M-2)"
    )
