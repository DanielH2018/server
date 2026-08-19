#!/usr/bin/env python3
"""Guards on the restore drill and the check that watches it.

"The backup completed" and "the backup restores" are different claims, and until 2026-08-19 only
the first was ever checked. The first drill ran by hand on 2026-08-15 and FAILED — not on the
data, but on a B2 Class-B cap at 100%, which surfaced as `cannot find volume.cfg in backupstore`
and reads exactly like data loss. The retry passed the next day. Then nothing recurred: the drill
was a one-off playbook in a home directory, untracked, unscheduled and unwatched.

Three properties carry the weight, and each is a way the drill can look healthy while being
useless:

PINNED LITERALS. The one-off pinned the backup ID, the volume name and the size, all true only on
the night it ran. A pinned backup ID is the worst: retention deletes it, and every later run then
fails on a missing backup while reporting a restore failure. Everything must be resolved live.

STAMP AFTER ASSERT. Check 7 reads the stamp's contents as the proof. Written before the data
assertions — or on any failure path — a drill that restores an empty volume stamps success.

FAIL CLOSED. `[[ -f $STAMP ]] && check_age` reports green when the drill has never run once,
which is the state most in need of reporting. Missing and unparseable stamps must both page.

Run: uv run pytest ansible/tests/test_longhorn_restore_drill.py
"""

import re
from pathlib import Path

import yaml

ANSIBLE = Path(__file__).resolve().parents[1]
K3S = ANSIBLE / "roles" / "setup" / "k3s"
DRILL = K3S / "templates" / "longhorn-restore-drill.sh.j2"
HEALTH = K3S / "templates" / "longhorn-backup-health.sh.j2"
CRONS = K3S / "tasks" / "health-crons.yml"


def _code(path: Path) -> str:
    """The script minus its comments — they discuss the rejected idioms on purpose."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _check_seven() -> str:
    code = _code(HEALTH)
    return code[code.index("DRILL_STAMP=") :]


def test_drill_resolves_the_backup_instead_of_pinning_one() -> None:
    """A pinned backup ID dies the day retention deletes it. See the module docstring."""
    code = _code(DRILL)
    assert "get backups.longhorn.io" in code and "sort_by" in code, (
        "the drill must resolve the newest Completed backup at run time"
    )
    assert not re.search(r"backup-[0-9a-f]{16}", code), (
        "a literal backup ID is pinned; retention will delete it and the drill will then "
        "fail forever while reporting a restore failure"
    )


def test_drill_resolves_volume_and_size_from_the_cluster() -> None:
    """The one-off hardcoded both. A resized volume would silently restore into the wrong size."""
    code = _code(DRILL)
    assert "jsonpath='{.spec.volumeName}'" in code, "resolve the volume from the PVC"
    assert "jsonpath='{.spec.size}'" in code, "resolve the size from the live Volume"
    assert not re.search(r"\bpvc-[0-9a-f]{8}-", code), (
        "a literal Longhorn volume name is pinned"
    )
    assert "134217728" not in code, "a literal size is pinned"


def test_drill_targets_a_b2_volume() -> None:
    """The daily tier routes to R2, so drilling it would prove the wrong store.

    traefik-acme, authelia-config, zigbee2mqtt-data and home-assistant-config all carry
    `spec.backupTargetName: r2`. B2 is the target under a transaction cap and the one this whole
    effort is about; a drill against an R2 volume restores from Cloudflare and says nothing about
    it. The original one-off drilled traefik-acme, which is R2.
    """
    defaults = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
    r2 = set(defaults.get("k3s_longhorn_r2_volumes", []))
    assert defaults["k3s_longhorn_restore_drill_pvc"] not in r2, (
        "the drill volume is routed to R2 — it would prove Cloudflare's store, not B2's"
    )


def test_drill_verification_is_not_tied_to_one_volume() -> None:
    """A filename-specific check cannot be repointed, which is how the one-off got stuck."""
    code = _code(DRILL)
    assert "acme.json" not in code, (
        "a volume-specific filename is baked in; the drill must work for whichever PVC "
        "k3s_longhorn_restore_drill_pvc names"
    )
    assert "files=" in code and "bytes=" in code, (
        "verification must count files and bytes, so it holds for any volume"
    )


def test_drill_tears_down_on_every_exit_path() -> None:
    """A half-restored volume left attached blocks the next run and occupies real disk."""
    code = _code(DRILL)
    assert "trap cleanup EXIT" in code, (
        "cleanup must run via a trap, not only on the success path — the drill exits early "
        "on every resolution failure"
    )
    for obj in ("delete pod", "delete pvc", "delete pv", "delete volume"):
        assert obj in code, f"cleanup must remove the drill's {obj.split()[-1]}"


def test_drill_stamps_only_after_the_assertions_pass() -> None:
    """The stamp IS the proof check 7 reads. Stamping early makes a broken drill read healthy."""
    code = _code(DRILL)
    assert code.count('date +%s >"$STAMP"') == 1, (
        "the stamp must be written in exactly one place"
    )
    stamp_at = code.index('date +%s >"$STAMP"')
    last_assert = max(code.index('"$FILES" -gt 0'), code.index('"$BYTES" -ge'))
    assert stamp_at > last_assert, (
        "the stamp is written before the data assertions, so a restored-but-empty volume "
        "would record a successful drill"
    )


def test_drill_rejects_an_empty_restore() -> None:
    """An empty filesystem mounts perfectly well and passes any mount-only check."""
    code = _code(DRILL)
    assert '"$FILES" -gt 0' in code, "a restore producing no files must fail"
    assert "MIN_BYTES" in code, (
        "a byte floor must guard against a mounted-but-empty volume"
    )


def test_drill_never_prints_the_restored_file() -> None:
    """These are service config volumes and several hold credentials — count, never print."""
    code = _code(DRILL)
    assert "wc -c" in code, "the probe must measure bytes, not emit them"
    assert "cat /drill" not in code, "the probe must never print a restored file"


def test_drill_distinguishes_no_output_from_an_empty_volume() -> None:
    """The first real run failed here, and the message could not say why.

    `kubectl run --rm -i` returned an EMPTY string on 2026-08-19 — no pod output and no error —
    so the drill reported "restored volume has no files:" with nothing after the colon. An empty
    capture and an empty volume are the two things this check exists to tell apart, and the
    attach form collapses them. The pod is now created, waited for, and read via logs.
    """
    code = _code(DRILL)
    assert "--rm -i" not in code, (
        "the attach form silently returns an empty capture; create the pod and read its logs"
    )
    assert "logs" in code and ".status.phase" in code, (
        "the probe must reach a terminal phase and be read from logs, so a failure is legible"
    )
    assert "produced no output" in code, (
        "an empty capture must fail with its own message, never as 'no files'"
    )


def test_drill_reports_a_failed_probe_pod_distinctly() -> None:
    """A pod that crashed says nothing about the volume — do not blame the data for it."""
    code = _code(DRILL)
    assert 'fail "probe pod ${PHASE}' in code, (
        "a non-Succeeded probe must report the phase"
    )


def test_stamp_is_readable_by_the_user_that_checks_it() -> None:
    """Writer and reader are different users, and root's umask here does not accommodate that.

    The drill runs as root; the heartbeat runs as sys_user under its own cron. Root's umask on
    daniel-box is 027, so plain `mkdir -p` produced drwxr-x--- root:root and the stamp was
    unreadable by the checker. Check 7 fails closed on an unreadable stamp, so the first GREEN
    drill would have paged "no restore drill has ever succeeded" — permanently, and precisely
    backwards. Observed 2026-08-19 on the first passing run.
    """
    code = _code(DRILL)
    assert 'chmod 0755 "$STAMP_DIR"' in code, (
        "the stamp directory mode must be explicit; root's umask makes it unreadable otherwise"
    )
    assert 'chmod 0644 "$STAMP"' in code, (
        "the stamp file must be readable by the checker"
    )


def test_drill_and_heartbeat_deploy_under_a_shared_tag() -> None:
    """Check 7 and the drill are one feature; shipping either alone pages for nothing.

    Deploying `restore-drill` alone installs a drill nothing watches. Deploying `backup-health`
    alone installs a check whose stamp no drill writes, which then pages forever. Both tasks
    carry `backup-health` so the pair moves together.
    """
    names = {
        "Deploy the Longhorn restore drill",
        "Schedule the Longhorn restore drill",
        "Deploy the Longhorn backup-plane heartbeat",
    }
    for task in _tasks():
        if task.get("name") in names:
            assert "backup-health" in task.get("tags", []), (
                f"{task['name']} must carry the backup-health tag so the drill and the check "
                "that reads its stamp are never deployed apart"
            )


def test_check_seven_fails_closed_when_the_drill_never_ran() -> None:
    """The never-run state is the one most in need of reporting. See the module docstring."""
    block = _check_seven()
    assert 'if [[ ! -r "$DRILL_STAMP" ]]; then' in block, (
        "a missing stamp must take the failing branch, not be skipped"
    )
    assert re.search(r'add 3 "no restore drill has ever succeeded', block), (
        "a never-run drill must page, not read green"
    )


def test_check_seven_rejects_an_unparseable_stamp() -> None:
    """A truncated or corrupt stamp is not evidence of a recent restore."""
    block = _check_seven()
    assert "=~ ^[0-9]+$" in block, (
        "the stamp contents must be validated before being compared"
    )
    assert block.count("add 3") >= 3, (
        "missing, unparseable and stale stamps are three distinct failures and each must page"
    )


def test_check_seven_pages_below_backup_failure_severity() -> None:
    """A stale drill is an assurance gap, not an active backup failure — rank 3, not 2."""
    block = _check_seven()
    assert "add 2" not in block, (
        "rank 2 means 'a backup actively failed'; a stale drill would then outrank a real "
        "failure in the pushed message, which surfaces only the top-ranked problem"
    )


def _tasks():
    docs = yaml.safe_load(CRONS.read_text())
    return docs if isinstance(docs, list) else []


def test_drill_is_scheduled_as_root() -> None:
    """It creates and deletes cluster objects; the read-only SA is Forbidden on all of it."""
    cron = next(
        (t for t in _tasks() if t.get("name") == "Schedule the Longhorn restore drill"),
        None,
    )
    assert cron is not None, (
        "the drill must be scheduled, or it is a script nobody runs"
    )
    assert cron["ansible.builtin.cron"]["user"] == "root", (
        "the drill needs cluster write access, which the heartbeat's read-only SA lacks"
    )


def test_drill_is_deployed_wherever_the_heartbeat_is() -> None:
    """Check 7 reads a stamp only the drill writes, so shipping one without the other pages."""
    deploy = next(
        (t for t in _tasks() if t.get("name") == "Deploy the Longhorn restore drill"),
        None,
    )
    assert deploy is not None
    assert "backup-health" in deploy.get("tags", []), (
        "deploying backup-health alone would add check 7 while leaving the drill absent, "
        "so the monitor would page for a drill that was never installed"
    )
