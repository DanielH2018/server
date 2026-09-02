#!/usr/bin/env python3
"""Guards on the etcd restore drill's schedule.

The off-box etcd snapshot has been taken, uploaded and alarmed since 2026-08-16. Nothing proved
it comes BACK on a schedule: `scripts/backup/etcd_restore_drill.sh` wrote a stamp that no code
read and that no cron kept fresh, so the off-box leg was verified once by hand on 2026-08-22 and
drifted from there (2026-08-28 review, M-2, open since 2026-08-23). etcd is the tier that carries
the Longhorn `Backup` CRs needed to FIND the volume backups, so it is the worse one to leave
unproven — the Longhorn plane beside it has had a scheduled drill and a fail-closed stamp reader
for weeks.

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

Run: uv run pytest ansible/tests/setup/test_etcd_restore_drill_cron.py
"""

from pathlib import Path

import yaml
from _helpers import ANSIBLE
from _helpers import load_yaml


K3S = ANSIBLE / "roles" / "setup" / "k3s"
CRONS = K3S / "tasks" / "health-crons.yml"
DRILL = Path(ANSIBLE).parent / "scripts" / "backup" / "etcd_restore_drill.sh"
CRON_TASK = "Schedule the etcd restore drill"


def _tasks() -> list[dict]:
    docs = load_yaml(CRONS)
    return docs if isinstance(docs, list) else []


def _cron_task() -> dict:
    task = next((t for t in _tasks() if t.get("name") == CRON_TASK), None)
    assert task is not None, (
        f"{CRON_TASK!r} is missing from health-crons.yml — without it the etcd snapshot is "
        "taken and alarmed but never restore-proven, which is the state this test exists for"
    )
    return task


def _defaults() -> dict:
    return yaml.safe_load((K3S / "defaults" / "main.yml").read_text())


def test_the_drill_is_scheduled() -> None:
    """The accepting half: a cron exists at all."""
    cron = _cron_task()["ansible.builtin.cron"]
    assert cron.get("user") == "root", (
        "the drill reads /etc/rancher/k3s/etcd-s3.env (0600 root) and the k3s server token; "
        "the read-only ServiceAccount cannot see either"
    )
    assert "etcd-snapshot" in _cron_task().get("tags", []), (
        "the drill must ship with the tag that deploys the R2 credentials it reads, or a "
        "tag-scoped run installs the cron without the etcd-s3.env it needs"
    )


def test_only_the_list_only_mode_is_scheduled() -> None:
    """The rejecting half, and the one that matters.

    A cron invoking the full drill would leave a restored etcd database and a cluster-token copy
    in /var/tmp on every failure, for 30 days, on a host where the full drill cannot pass.
    """
    job = _cron_task()["ansible.builtin.cron"]["job"]
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


def test_the_job_sets_a_path_that_reaches_k3s() -> None:
    """Cron inherits no PATH, and the drill's preflight is `command -v k3s`."""
    job = _cron_task()["ansible.builtin.cron"]["job"]
    assert "PATH=" in job, (
        "without an explicit PATH the drill dies at `command -v k3s` and reports k3s missing on "
        "a host that has it"
    )
    assert "/usr/local/bin" in job.split("PATH=", 1)[1].split()[0], (
        "k3s lives in /usr/local/bin; a PATH that omits it defeats the point of setting one"
    )


def test_the_drill_can_be_disarmed_without_deleting_the_task() -> None:
    """A one-way arming switch is a bug: adding a way in means adding the way out."""
    cron = _cron_task()["ansible.builtin.cron"]
    state = str(cron.get("state", "present"))
    assert "k3s_etcd_restore_drill_armed" in state, (
        "state: must follow k3s_etcd_restore_drill_armed, or disarming the drill means "
        "deleting the task rather than flipping a flag"
    )
    assert "absent" in state, (
        "the disarmed branch must remove the cron, not leave it installed"
    )
    assert _defaults()["k3s_etcd_restore_drill_armed"] is True, (
        "the drill ships armed; a default-off drill is the unproven state this closes"
    )


def test_the_cadence_is_weekly_and_clear_of_every_backup_window() -> None:
    """Contending with a backup window would make the drill the thing that broke the backup."""
    minute, hour, dom, month, dow = _defaults()["k3s_etcd_restore_drill_cron"].split()
    assert (dom, month) == ("*", "*")
    assert dow != "*", (
        "weekly, not daily: the snapshot is daily and alarmed, so this proves the RESTORE leg, "
        "and a daily download of the full snapshot buys nothing the weekly one does not"
    )
    busy = {
        2,
        3,
        4,
        5,
        6,
    }  # 02:45 snapshot, 03:30/04:30 Longhorn, 04:10 drill, 05:15 prune, 06:10 trim
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

    The cron now exists, so the block must no longer claim the drill is never scheduled — a stale
    precondition is how the next reviewer re-derives a closed decision.
    """
    header = DRILL.read_text()
    assert "never deployed to a cron target" not in header, (
        "the drill IS now scheduled by health-crons.yml; leaving that claim in place would tell "
        "the next reader the reader-half is still blocked when it is not"
    )
    assert "monitor-bridge/check.py" in header, (
        "the block must keep naming check.py as the reader's owner — longhorn-backup-health.sh "
        "would be reading a second drill's evidence out of the first drill's watchdog"
    )
    for stale in (
        "it is not yet written",
        "When the reader lands",
        "has no cron",
    ):
        assert stale not in header, (
            f"the header still says {stale!r}. The reader shipped in PR #535 as "
            "check_etcd_restore_drill and the cron in PR #531; a stale precondition here is "
            "what made issue #800 get re-observed after both halves were already done"
        )
