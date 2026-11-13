#!/usr/bin/env python3
"""Guards on the etcd restore drill's schedule.

The off-box etcd snapshot is taken, uploaded and alarmed, and only a scheduled drill proves
it comes BACK. `scripts/backup/etcd_restore_drill.sh` writes a stamp, and the cron keeps that
stamp fresh. etcd is the tier that carries the Longhorn `Backup` CRs needed to FIND the
volume backups, so it is the worse one to leave unproven.

Three properties carry the weight here, and two of them are ways this cron could be actively
worse than no cron at all:

MODE. Only `--list-only` may be scheduled. It proves the off-box leg end to end — credentials,
bucket, folder, download, decompression. The FULL drill does not pass on daniel-box and cannot:
five structural `k3s server --cluster-reset` failures are documented in the script's own header.
Scheduling it would be strictly harmful rather than merely useless, because `cleanup()` sets
KEEP=1 on ANY non-zero exit while scratch dirs are swept only at +30 days — so a recurring failing
full drill holds one to two restored etcd databases plus cluster-token copies in /var/tmp as a
steady state. The script records at its sweep block that this accumulation is bounded ONLY because
the drill has no cron. A cadence that inverts its own stated bound is not a fix.

PATH. Cron inherits neither PATH nor KUBECONFIG. The drill's preflight is `command -v k3s`, and
k3s lives in /usr/local/bin, so without an explicit PATH the drill dies reporting k3s missing on
a host that has it — the failure reads as a broken cluster rather than a broken cron.

BOTH DIRECTIONS. A drill that can be armed must be disarmable, or turning it off means deleting
the task. `state:` must follow the armed flag rather than being hardcoded `present`.

Run: uv run pytest hosts/tests/test_etcd_restore_drill_cron.py
"""

import json

import pytest

from _helpers import (
    REPO,
    K8S_ROLES,
)
from _helpers import load_defaults
from lib import yaml_fast
from lib.repo_paths import K3S_HOST_CHECKS
from renderer.renderer_lib import host as host_mode


DRILL = REPO / "scripts" / "backup" / "etcd_restore_drill.sh"
READER = K8S_ROLES / "monitor-bridge/files/checks/service.py"
PAGING_RULES = K8S_ROLES / "observability/files/rules/bridge-exporter.yaml"
SERVICE = "/usr/lib/systemd/system/homelab-etcd-restore-drill.service"
TIMER = "homelab-etcd-restore-drill.timer"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def _service(package) -> str:
    body = package.files[SERVICE][0]
    return body.decode() if isinstance(body, bytes) else body


def _exec_start(package) -> str:
    return next(
        line for line in _service(package).splitlines() if line.startswith("ExecStart=")
    )


def test_the_drill_is_scheduled(package) -> None:
    """The accepting half: the timer ships and its enable link arms it."""
    manifest = package.files["/usr/lib/homelab/manifest.json"][0]
    assert TIMER in json.loads(manifest)["enable_links"]
    assert "\nUser=" not in _service(package), (
        "the drill reads /etc/rancher/k3s/etcd-s3.env (0600 root) and the k3s server token; "
        "the read-only ServiceAccount cannot see either"
    )


def test_only_the_list_only_mode_is_scheduled(package) -> None:
    """The rejecting half, and the one that matters.

    A unit invoking the full drill would leave a restored etcd database and a cluster-token copy
    in /var/tmp on every failure, for 30 days, on a host where the full drill cannot pass.
    """
    job = _exec_start(package)
    assert "--list-only" in job, (
        "only the list-only leg is provable on this host; see the five structural failures in "
        "scripts/backup/etcd_restore_drill.sh's header"
    )
    for banned in ("--full", "--keep"):
        assert banned not in job, (
            f"{banned} must never appear in a scheduled invocation: cleanup() sets KEEP=1 on any "
            "non-zero exit and scratch dirs are swept only at +30 days, so a recurring failure "
            "accumulates restored etcd databases in /var/tmp"
        )


def test_the_job_sets_a_path_that_reaches_k3s(package) -> None:
    """The drill's preflight is `command -v k3s`, and k3s lives in /usr/local/bin."""
    assert "Environment=PATH=/usr/local/bin:" in _service(package), (
        "without /usr/local/bin the drill dies at `command -v k3s` and reports k3s missing on "
        "a host that has it"
    )


def test_the_drill_can_be_disarmed_without_deleting_the_entries() -> None:
    """A one-way arming switch is a bug: adding a way in means adding the way out."""
    spec = yaml_fast.safe_load((K3S_HOST_CHECKS / "component.yml").read_text())
    assert spec["enable"][TIMER]["when"] == "k3s_etcd_restore_drill_armed"
    units = [e for e in spec["files"] if "homelab-etcd-restore-drill" in e["dst"]]
    assert len(units) == 2
    assert {e["when"] for e in units} == {"k3s_etcd_restore_drill_armed"}
    assert load_defaults(K3S_HOST_CHECKS)["k3s_etcd_restore_drill_armed"] is True, (
        "the drill ships armed; a default-off drill is the unproven state this closes"
    )


def test_the_cadence_is_weekly_and_clear_of_every_backup_window() -> None:
    """Contending with a backup window would make the drill the thing that broke the backup."""
    minute, hour, dom, month, dow = load_defaults(K3S_HOST_CHECKS)[
        "k3s_etcd_restore_drill_cron"
    ].split()
    assert (dom, month) == ("*", "*")
    assert dow != "*", (
        "weekly, not daily: the snapshot is daily and alarmed, so this proves the RESTORE leg, "
        "and a daily download of the full snapshot buys nothing the weekly one does not"
    )
    busy = {
        2,
        3,
        4,
        6,
    }  # 02:45 snapshot, 03:30/04:30 Longhorn, 04:10 drill, 06:10 trim
    assert int(hour) not in busy, (
        f"hour {hour} collides with a backup or prune window; the drill must not contend with "
        "the tier it is proving"
    )
    assert (int(hour), int(minute)) == (10, 20), (
        "Monday 10:20 — clear of every window above, in an hour nothing else uses, and in "
        "daylight so a failure is visible on a day someone is around"
    )


def test_the_scripts_ordering_note_still_matches_reality() -> None:
    """The script's DECIDED block sequenced cron-then-reader.

    The cron exists, so the block must not claim the drill is never scheduled — a stale
    precondition is how the next reviewer re-derives a closed decision.
    """
    header = DRILL.read_text()
    assert "never deployed to a cron target" not in header, (
        "the drill IS now scheduled by the k3s-host-checks component; leaving that claim in place would tell "
        "the next reader the reader-half is still blocked when it is not"
    )
    assert "monitor-bridge/files/checks/service.py" in header, (
        "the block must keep naming monitor-bridge as the reader's owner — "
        "longhorn-backup-health.sh would be reading a second drill's evidence out of the first "
        "drill's watchdog"
    )
    assert "def check_etcd_restore_drill(" in READER.read_text(), (
        "the block names this file as the reader's home, and the reader is not in it"
    )
    assert "EtcdRestoreDrillFailing" in header, (
        "the block must name the alert rule that pages on the reader's verdict"
    )
    assert "alert: EtcdRestoreDrillFailing" in PAGING_RULES.read_text(), (
        "the block names an alert rule that bridge-exporter.yaml does not define"
    )
    for stale in (
        "it is not yet written",
        "When the reader lands",
        "has no cron",
        "Kuma monitor",
    ):
        assert stale not in header, (
            f"the header still says {stale!r}. The reader shipped in PR #535 as "
            "check_etcd_restore_drill and the cron in PR #531; a stale precondition here is "
            "what made issue #800 get re-observed after both halves were already done"
        )
