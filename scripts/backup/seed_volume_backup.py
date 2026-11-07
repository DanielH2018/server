#!/usr/bin/env python3
"""Give one Longhorn volume its first backup now, instead of waiting for its recurring job.

DRY RUN BY DEFAULT. Without --apply it makes every read and refusal and prints what it would
create. docs/longhorn-disaster-recovery.md names when to run it.

WHY IT EXISTS. A volume rebuilt by a block-size migration has no backup history, and a weekly
shard runs on one weekday, so the replacement waits up to six days for a recovery point. The
backup-health check's first-run grace does not page for that.

WHAT IT COSTS. One full backup of the used blocks, against `k3s_longhorn_daily_backup_budget`.
Seed one volume at a time, and read the budget line in
`journalctl -t longhorn-backup-health` before seeding more than a few.

WHAT IT REFUSES. An unbound claim. A volume in no backup tier, because no job's retain would
ever prune the backup. A disarmed or unavailable target, because the Backup could never
complete. A volume that already has a backup, unless --allow-existing says its recurring run
was missed. A group no RecurringJob selects.

WHY CRS AND NOT THE API. longhorn-manager's NetworkPolicy leaves the backend ClusterIP
unreachable from a host, and these CRs are what the API's snapshotBackup action creates.

Run from the repo root on daniel-box. A dry run reads through your own kubeconfig, and an apply
needs the root-only one:
    uv run python scripts/backup/seed_volume_backup.py --claim <pvc>
    sudo .venv/bin/python -B scripts/backup/seed_volume_backup.py --claim <pvc> --apply
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(
    0, str(_Path(__file__).resolve().parents[2] / "hosts" / "lib")
)  # host_lib
from lib.cli_help import ArgumentParser, error
from lib.exit_codes import FAILED, OK
from lib.kubectl import MissingKubectl
from lib.operator_kubectl import Kubectl, KubectlFailed, for_mode
from longhorn_lib.longhorn_backups import (
    B2_TARGET,
    GROUP_LABEL_PREFIX,
    NO_BACKUP_GROUP,
)

LONGHORN_NAMESPACE = "longhorn-system"
APPLY_COMMAND = "scripts/backup/seed_volume_backup.py <the same flags> --apply"
SEEDED_BY = "seed_volume_backup.py"
SNAPSHOT_RETRIES = 30
SNAPSHOT_DELAY_S = 10
BACKUP_DELAY_S = 15
BACKUP_TIMEOUT_S = 900


class SeedRefused(Exception):
    """A read found a reason not to seed. Nothing has been changed."""


class SeedFailed(Exception):
    """A seed started and did not finish."""


def tier_group(volume: dict) -> str:
    """The recurring-job group a volume's labels put it in, or "" for none."""
    labels = (volume.get("metadata") or {}).get("labels") or {}
    keys = sorted(k for k in labels if k.startswith(GROUP_LABEL_PREFIX))
    return keys[0].removeprefix(GROUP_LABEL_PREFIX) if keys else ""


def owning_job(jobs: list[dict], group: str) -> str | None:
    """The RecurringJob whose first group is `group`.

    Read from the CRs, because the daily tier's group is `default` and its job `daily-backup`.
    """
    names = sorted(
        job["metadata"]["name"]
        for job in jobs
        if ((job.get("spec") or {}).get("groups") or [None])[0] == group
    )
    return names[0] if names else None


def seed_name(volume: str, existing: int, now: datetime) -> str:
    """The seed's Snapshot and Backup name.

    A forced seed adds a timestamp, because `kubectl apply` on the fixed name would adopt an
    earlier seed's Completed Backup and report it as the new recovery point.
    """
    name = f"seed-{volume.removeprefix('pvc-')}"
    return f"{name}-{now.strftime('%Y%m%d%H%M%S')}" if existing else name


def snapshot_manifest(name: str, volume: str, job: str) -> dict:
    """The Snapshot. `RecurringJob` is what lets that job's cleanup delete it."""
    return {
        "apiVersion": "longhorn.io/v1beta2",
        "kind": "Snapshot",
        "metadata": {"name": name, "namespace": LONGHORN_NAMESPACE},
        "spec": {
            "volume": volume,
            "createSnapshot": True,
            "labels": {"RecurringJob": job, "seeded-by": SEEDED_BY},
        },
    }


def backup_manifest(name: str, volume: str, target: str, job: str) -> dict:
    """The Backup. Without its two metadata labels it reconciles against the default target."""
    return {
        "apiVersion": "longhorn.io/v1beta2",
        "kind": "Backup",
        "metadata": {
            "name": name,
            "namespace": LONGHORN_NAMESPACE,
            "labels": {"backup-target": target, "backup-volume": volume},
        },
        "spec": {
            "snapshotName": name,
            "labels": {"RecurringJob": job, "seeded-by": SEEDED_BY},
        },
    }


class Seed:
    """One claim's seed.

    Attributes:
        kubectl: runs kubectl.
        sleep: waits between polls.
        now: reads the UTC time for a forced seed's name.
    """

    def __init__(
        self,
        kubectl: Kubectl,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.kubectl = kubectl
        self.sleep = sleep
        self.now = now

    def _get(self, *args: str) -> dict:
        return json.loads(
            self.kubectl("-n", LONGHORN_NAMESPACE, "get", *args, "-o", "json")
        )

    def plan(self, namespace: str, claim: str, allow_existing: bool) -> dict:
        """Make every read and refusal, and return what a seed would create.

        Raises:
            SeedRefused: a refusal the module docstring names.
        """
        pv = self.kubectl(
            "-n", namespace, "get", "pvc", claim, "-o", "jsonpath={.spec.volumeName}"
        ).strip()
        if not pv:
            raise SeedRefused(
                f"{claim} in {namespace} is not bound to a PersistentVolume, so there is "
                "nothing to snapshot."
            )
        volume = self._get("volumes.longhorn.io", pv)
        group = tier_group(volume)
        if not group or group == NO_BACKUP_GROUP:
            raise SeedRefused(
                f"{claim} is in recurring-job group '{group or '(none)'}', so nothing would "
                "ever prune the backup this creates. The longhorn-reconcile CronJob labels a "
                "volume into a tier."
            )
        target = (volume.get("spec") or {}).get("backupTargetName") or B2_TARGET
        state = self._get("backuptargets.longhorn.io", target)
        url = (state.get("spec") or {}).get("backupTargetURL")
        available = (state.get("status") or {}).get("available")
        if not url or available is not True:
            raise SeedRefused(
                f"backup target '{target}' is disarmed or unavailable (URL {url!r}, available "
                f"{available!r}), so this backup could not complete."
            )
        existing = sum(
            1
            for item in self._get("backups.longhorn.io")["items"]
            if (item.get("status") or {}).get("volumeName") == pv
        )
        if existing and not allow_existing:
            raise SeedRefused(
                f"{claim} already has {existing} backup(s); its recurring job covers it. Pass "
                "--allow-existing to back it up anyway."
            )
        job = owning_job(self._get("recurringjobs.longhorn.io")["items"], group)
        if job is None:
            raise SeedRefused(
                f"no RecurringJob lists group '{group}', so nothing would ever prune this seed."
            )
        return {
            "claim": claim,
            "volume": pv,
            "group": group,
            "job": job,
            "target": target,
            "robustness": (volume.get("status") or {}).get("robustness", "unknown"),
            "name": seed_name(pv, existing, self.now()),
        }

    def _apply(self, manifest: dict) -> None:
        self.kubectl("apply", "-f", "-", stdin=json.dumps(manifest))

    def create(self, plan: dict) -> None:
        """Create the Snapshot, wait for it, create the Backup, and wait for that.

        A failed read polls again until its wait runs out, because Longhorn keeps uploading
        whether or not this script is watching.

        Raises:
            SeedFailed: a wait ran out, or the Backup ended in Error.
        """
        name = plan["name"]
        self._apply(snapshot_manifest(name, plan["volume"], plan["job"]))
        unread = ""
        for _ in range(SNAPSHOT_RETRIES):
            try:
                snapshot = self._get("snapshots.longhorn.io", name)
            except KubectlFailed as err:
                unread = f" Its last read failed: {err}."
            else:
                unread = ""
                if (snapshot.get("status") or {}).get("readyToUse") is True:
                    break
            self.sleep(SNAPSHOT_DELAY_S)
        else:
            raise SeedFailed(
                f"snapshot {name} was not ready after "
                f"{SNAPSHOT_RETRIES * SNAPSHOT_DELAY_S}s.{unread} No Backup was requested."
            )
        self._apply(backup_manifest(name, plan["volume"], plan["target"], plan["job"]))
        for _ in range(BACKUP_TIMEOUT_S // BACKUP_DELAY_S):
            try:
                status = self._get("backups.longhorn.io", name).get("status") or {}
            except KubectlFailed as err:
                unread = f" Its last read failed: {err}."
            else:
                unread = ""
                if status.get("state") == "Completed":
                    return
                if status.get("state") == "Error":
                    raise SeedFailed(
                        f"the seed backup for {plan['claim']} ended in Error: "
                        f"{status.get('error', '')}. The Backup CR {name} is still present. "
                        "Delete it before retrying, or the apply adopts the failed one."
                    )
            self.sleep(BACKUP_DELAY_S)
        raise SeedFailed(
            f"the Backup CR {name} did not finish within {BACKUP_TIMEOUT_S}s.{unread} It is "
            "still present and may yet complete."
        )


def main(
    argv: list[str] | None = None,
    kubectl: Kubectl | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    """Parse the flags and seed the claim, or report what a seed would do.

    Args:
        argv: the command line; None reads `sys.argv`.
        kubectl: replaces the real runner, for a test.
        sleep: waits between polls.
        now: reads the UTC time for a forced seed's name.

    Returns:
        OK when it seeded or would seed, and FAILED on a refusal or a failed seed.
    """
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--claim", required=True, help="the PVC to seed")
    parser.add_argument("--namespace", default="homelab", help="the claim's namespace")
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="seed a volume that has a backup but missed its recurring run",
    )
    parser.add_argument(
        "--apply", action="store_true", help="seed; without it nothing changes"
    )
    args = parser.parse_args(argv)
    try:
        seed = Seed(kubectl or for_mode(args.apply, APPLY_COMMAND), sleep, now)
        plan = seed.plan(args.namespace, args.claim, args.allow_existing)
        print(
            f"seeding {plan['claim']} ({plan['volume']}), tier {plan['group']} "
            f"(job {plan['job']}), target {plan['target']}, "
            f"robustness {plan['robustness']}, as {plan['name']}"
        )
        if not args.apply:
            print("dry run: nothing changed. Re-run with --apply to seed.")
            return OK
        seed.create(plan)
    except SeedRefused as err:
        error(f"{err} Nothing has been changed.")
        return FAILED
    except (SeedFailed, KubectlFailed, MissingKubectl, ValueError) as err:
        error(str(err))
        return FAILED
    print(
        f"{plan['claim']} now has a recovery point on target {plan['target']}. Its "
        f"{plan['group']} recurring job takes over, and its retain prunes this backup."
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
