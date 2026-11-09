"""Every file a setup role or an installer writes where the AIDE check pages is listed as known.

The check pages on an unpackaged unit, apt source or key, MOTD script, init script or
/usr/local/sbin entry (`PERSIST` in aide-check.sh.j2). `ROLE_WRITTEN` names the files this
repo's tooling writes there, so a deploy does not page. No setup task writes under a paged
prefix since H7.63 emptied initial_setup, so the census of the roles' destinations retired with
its subject; the k3s installer's units and snapd's mounts are what the list must still cover.

Run: uv run pytest ansible/tests/setup/test_aide_check_role_written.py
"""

import re

from _aide_check_run import SCRIPT
from _shell_render import render_shell_script

# Files an installer or snapd writes, which no task names. k3s-install.sh rewrites the unit and
# its environment file on every args or version change; snapd adds a mount per snap revision.
INSTALLER_WRITTEN = (
    "/etc/systemd/system/k3s.service",
    "/etc/systemd/system/k3s.service.env",
    "/etc/systemd/system/k3s-agent.service",
    "/etc/systemd/system/k3s-agent.service.env",
    "/etc/systemd/system/snap-snapd-27738.mount",
)


def role_written_pattern() -> re.Pattern[str]:
    body = render_shell_script(
        *SCRIPT,
        overrides={"domain": "example.test", "k3s_metallb_ingress_vip": "10.0.0.240"},
    )
    found = re.search(r"^ROLE_WRITTEN='(.*)'$", body, re.MULTILINE)
    assert found, "aide-check.sh no longer defines ROLE_WRITTEN"
    return re.compile(found.group(1))


def unlisted(destinations: set[str], pattern: re.Pattern[str]) -> list[str]:
    return sorted(d for d in destinations if not pattern.search(d))


def test_the_installers_files_are_listed_as_role_written() -> None:
    assert unlisted(set(INSTALLER_WRITTEN), role_written_pattern()) == []


def test_a_unit_no_role_installs_is_not_listed() -> None:
    pattern = role_written_pattern()
    for path in (
        "/etc/systemd/system/unlisted.service",
        "/etc/systemd/system/gitops-deploy.service.d/override.conf",
        "/etc/apt/sources.list.d/extra.list",
        "/usr/local/sbin/helper",
        "/etc/systemd/system/kuma-check-live-drift.timer",
        "/etc/systemd/system/kuma-check-manifest-prune-boot.service",
        "/etc/systemd/system/kuma-check-render-records.timer",
        # H7.63 retired the setup-drift timer, so its units page again.
        "/etc/systemd/system/kuma-check-setup-drift.timer",
    ):
        assert unlisted({path}, pattern) == [path]
