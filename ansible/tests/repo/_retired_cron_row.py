"""The retired-crons census row: every host cron or fragment H2 retired keeps a legacy entry.

H2 retired eight daniel-box host crons and two setup-deployed fragments, and their absent arms
lived in `roles/setup/k8s_host_files` until H7.48 emptied it. Daniel-box's host package carries
them as `legacy:` entries since, and `homelab-reconcile` removes each from a host that still
holds it. A package that drops an entry leaves the thing on a host restored from an old backup
with nothing to remove it, and a stale fragment turns setup drift DOWN. The plan is
`docs/target-architecture/host/h7-delete-ansible.md`, H7.48. The row sits in its own module
because `test_census_rows_roles.py`, which imports it into `ROWS`, is at the cap for a test
module.
"""

import json

from _row_table import Census, Subject, tracked
from lib import yaml_fast

FRAGMENT_DIR = "/var/lib/homelab/setup-deployed-manifest.d/"
PACKAGE = "hosts/daniel-box/package.yml"

# Cron task name -> the `cron_file` its absent arm removed.
RETIRED_CRONS = {
    "Cloudflare IP drift": "cloudflare-ip-drift",
    "CrowdSec home allowlist": "crowdsec-home-allowlist",
    "CrowdSec remote allowlist": "crowdsec-remote-allowlist",
    "CrowdSec bouncer prune": "crowdsec-bouncer-prune",
    "CrowdSec AppSec verify": "crowdsec-appsec-verify",
    "configarr sync health": "configarr-health",
    "janitorr error health": "janitorr-health",
    "qbittorrent prefs drift check": "qbittorrent-prefs-check",
}
# The `stamp_deployed_name` of each retired fragment.
RETIRED_FRAGMENTS = ("configarr", "janitorr")


def _wanted() -> dict[str, dict[str, str]]:
    """The legacy entry each retired name needs, keyed by a label for the report."""
    wanted = {
        f"cron {name!r}": {"cron_file": cron_file}
        for name, cron_file in RETIRED_CRONS.items()
    }
    wanted |= {
        f"fragment {name!r}": {"path": FRAGMENT_DIR + name}
        for name in RETIRED_FRAGMENTS
    }
    return wanted


def missing_entries(subject: Subject) -> list[str]:
    """Each retired name whose legacy entry the package does not list."""
    legacy = (yaml_fast.safe_load(subject.text) or {}).get("legacy") or []
    return [
        f"retired {label} has no legacy entry {entry}"
        for label, entry in _wanted().items()
        if entry not in legacy
    ]


def _package(legacy: list[dict[str, str]]) -> Subject:
    return Subject(PACKAGE, text=json.dumps({"legacy": legacy}))


_ALL = list(_wanted().values())

RETIRED_CRON_ARMS = Census(
    name="retired-host-crons-keep-a-legacy-entry",
    reason=(
        "A host cron or setup-deployed fragment H2 retired is removed only by its legacy entry "
        "in daniel-box's package, which homelab-reconcile retires on every run. Drop the entry "
        "and a host that still holds the cron keeps running it with no writer, and a stale "
        "fragment turns setup drift DOWN. Keep the entry until H7 retires the census."
    ),
    files=lambda: tracked(PACKAGE),
    offence=missing_entries,
    red=(
        # A package that lost the Cloudflare IP drift cron's entry.
        _package([e for e in _ALL if e != {"cron_file": "cloudflare-ip-drift"}]),
        # janitorr's fragment entry dropped.
        _package([e for e in _ALL if e != {"path": FRAGMENT_DIR + "janitorr"}]),
        # The cron named by its task name rather than its cron file: reconcile would look for
        # an `#Ansible:` marker in the user's crontab and leave /etc/cron.d alone.
        _package(
            [e for e in _ALL if e != {"cron_file": "configarr-health"}]
            + [{"cron": "configarr sync health", "user": "ubuntu"}]
        ),
    ),
    green=(_package(_ALL + [{"path": "/usr/local/bin/x.sh"}]),),
    must_find=frozenset({PACKAGE}),
)
