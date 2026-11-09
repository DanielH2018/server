"""No systemd unit template in the repo may interpolate a secret into ExecStart.

systemd serves unit content over the system bus, so `systemctl show <unit> -p ExecStart`
prints the rendered command to any local user regardless of the file's mode. A tree walk
rather than an enumeration, so a new unit cannot inherit the shape unseen.
"""

# ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py

import pathlib
import re

_ROLES = pathlib.Path(__file__).parents[3]
# The host components' unit templates, the claude-code component's moved role units among them.
_COMPONENTS = _ROLES.parents[1] / "hosts" / "components"

# A Jinja interpolation naming a secret. Matched on the VARIABLE NAME, not on "does this
# ExecStart interpolate anything" — the broad form flags six legitimate units that interpolate
# a path or a username (claude-rc.service.j2, both gitops-deploy units, both renovate-notify
# units, one retired archive/ template). Only the secret-named form isolates a real leak.
_SECRET_VAR = re.compile(
    r"\{\{[^}]*\b\w+(?:_webhook|_token|_password|_secret|_key)\b[^}]*\}\}"
)


def _unit_templates() -> list[pathlib.Path]:
    """Every systemd unit template in the repo, minus retired code.

    A TREE WALK, not an enumeration. A guard that names its paths can only prove the units its
    own fix touched, so it derives its own corpus.
    """
    return sorted([*_ROLES.rglob("*.service.j2"), *_COMPONENTS.rglob("*.service.j2")])


def test_no_unit_template_embeds_a_secret_in_execstart():
    # A unit that interpolates the SOPS webhook straight into ExecStart cannot rely on
    # `mode: 0600` to protect it: systemd serves unit content over the system bus, so
    # `systemctl show <unit> -p ExecStart` prints the full webhook URL to any local user with
    # no sudo. With EnvironmentFile the same command prints the literal ${ALERT_WEBHOOK}.
    #
    # A comment claiming the protection is the least reliable evidence in the file, so the claim
    # gets a test — and the test derives which files it covers rather than being told.
    units = _unit_templates()
    assert units, (
        f"No *.service.j2 found under {_ROLES} — the walk is broken, not the tree."
    )
    assert _COMPONENTS / "claude-code" / "templates" / "claude-rc.service.j2" in units
    for unit_path in units:
        unit = unit_path.read_text()
        exec_start = re.search(
            r"^ExecStart=.*?(?=\n(?!\s)|\Z)", unit, re.MULTILINE | re.DOTALL
        )
        if not exec_start:
            continue  # a .timer-adjacent or Type=oneshot-less unit; nothing to leak
        leaked = _SECRET_VAR.search(exec_start.group(0))
        assert not leaked, (
            f"{unit_path.relative_to(_ROLES.parents[1])} interpolates {leaked.group(0)} into ExecStart. "
            f"`systemctl show` will print it to any local user regardless of the unit file's "
            f'mode. Reference it as "${{ALERT_WEBHOOK}}" and supply it with EnvironmentFile= '
            f"instead."
        )
