#!/usr/bin/env python3
"""Guards on the restore drill and the check that watches it.

"The backup completed" and "the backup restores" are different claims, and until 2026-08-19 only
the first was ever checked. The first drill ran by hand on 2026-08-15 and FAILED — not on the
data, but on a B2 Class-B cap at 100%, which surfaced as `cannot find volume.cfg in backupstore`
and reads exactly like data loss. The retry passed the next day. Then nothing recurred: the drill
was a one-off playbook in a home directory, untracked, unscheduled and unwatched.

Four properties carry the weight, and each is a way the drill can look healthy while being
useless:

COVERAGE. The drill rotates over every volume declared for backup (one per night since
2026-08-20), so a green check 7 means one of ~25 volumes restored — not all of them. A volume
that fails every time its slot comes up hides behind the ones that pass, which is what check 8
and the per-volume stamps exist to catch. Rotation must also order by ATTEMPT, not by success, or
one broken volume is picked every night and coverage collapses to that volume alone.

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
from _helpers import ANSIBLE


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
    assert "vol: .metadata.name" in code, "resolve the volume from the live Volume list"
    assert "size: (.spec.size | tostring)" in code, (
        "resolve the size from the live Volume"
    )
    assert not re.search(r"\bpvc-[0-9a-f]{8}-", code), (
        "a literal Longhorn volume name is pinned"
    )
    assert "134217728" not in code, "a literal size is pinned"


def test_drill_rotates_over_the_declared_backup_set() -> None:
    """Coverage is the point of rotating: one volume proves one volume, not the fleet.

    Eligibility must come from the `recurring-job-group.longhorn.io/*` LABEL — the same selector
    check 4 uses, and the only one that cannot drift from what the RecurringJobs really select. A
    PVC's storageClassName is immutable and still reads `longhorn` on volumes dropped from the
    backup set on 2026-08-08, so filtering by class would drill volumes nothing backs up.
    """
    defaults = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
    assert defaults["k3s_longhorn_restore_drill_pvc"] == "", (
        "the drill is pinned to one volume — rotation is disabled and 24 volumes go unproven"
    )
    code = _code(DRILL)
    assert "recurring-job-group.longhorn.io/" in code, (
        "eligibility must be selected by the recurring-job-group label"
    )
    assert 'select(. != "no-backup")' in code, (
        "the no-backup group is an explicit opt-out and must be excluded"
    )
    assert "storageClassName" not in code.split("cat >")[0], (
        "eligibility is filtered by storage class, which is immutable and drifts"
    )


def test_rotation_selects_by_attempt_not_by_success() -> None:
    """Selecting on last SUCCESS lets one broken volume starve the other 24.

    A volume that fails every drill would stay the least-recently-succeeded forever, be chosen
    every night, and the rotation would never advance — turning a one-volume fault into total
    loss of coverage. Stamping the attempt before the restore is what bounds a failing volume to
    one slot per cycle.
    """
    code = _code(DRILL)
    assert 'at=$(stat -c %Y "${ATTEMPT_DIR}/${pvc}"' in code, (
        "rotation must order candidates by their attempt stamp"
    )
    attempt_at = code.index('touch "${ATTEMPT_DIR}/${PVC}"')
    assert attempt_at < code.index("$KUBECTL apply -f"), (
        "the attempt stamp must be written BEFORE the restore, or a failing volume starves "
        "the rotation"
    )
    assert code.index('cp "$STAMP"') > code.index('"$BYTES" -ge "$MIN_BYTES"'), (
        "the per-volume success stamp must be written only after the assertions pass"
    )


def test_drill_carries_the_source_block_size() -> None:
    """Omitted, the restore volume takes the GLOBAL block size, which is 16 MiB since 2026-08-19.

    The four R2 volumes are still 2 MiB and only convert on recreation. Longhorn refuses a volume
    size that is not a multiple of its block size, so a mismatch strands the restore on exactly
    the volumes rotation added.
    """
    code = _code(DRILL)
    assert 'backupBlockSize: "${BLOCKSIZE}"' in code, (
        "the restore volume must carry the source volume's block size"
    )


def test_drill_publishes_its_candidate_list() -> None:
    """Check 8 has no cluster credential worth the name, and a second definition would drift."""
    code = _code(DRILL)
    assert 'cut -f1 >"$CANDIDATES"' in code, (
        "the drill must publish the eligible set for the heartbeat's coverage check"
    )
    assert 'chmod 0644 "$CANDIDATES"' in code, (
        "the heartbeat runs as a different user and must be able to read it"
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


def test_drill_runs_daily() -> None:
    """A monthly drill leaves a broken restore path undetected for weeks."""
    defaults = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
    minute, hour, dom, month, dow = defaults["k3s_longhorn_restore_drill_cron"].split()
    assert (dom, month, dow) == ("*", "*", "*"), (
        "the drill must run every night for the rotation to cover the fleet"
    )
    assert (int(hour), int(minute)) == (4, 10), (
        "04:10 sits after the 03:30 daily tier, clear of the 04:30 weekly shards and of the "
        "05:15 manifest-prune window that would see the drill's transient objects"
    )


def test_cadence_and_staleness_window_move_together() -> None:
    """Check 7's stamp age is the drill's ONLY alert path — logger and stderr reach nobody.

    Raising the cadence without lowering the window buys zero detection latency, which is the
    trap this pairing exists to prevent.
    """
    defaults = yaml.safe_load((K3S / "defaults" / "main.yml").read_text())
    max_age = defaults["k3s_longhorn_restore_drill_max_age_days"]
    assert 2 <= max_age <= 7, (
        f"max_age_days is {max_age}: a nightly drill should tolerate a couple of bad nights, "
        "not a month of silence"
    )


def test_check_eight_derives_its_window_from_the_fleet() -> None:
    """A hardcoded coverage window goes stale the moment a volume joins the backup set."""
    block = _check_seven()
    assert "CAND_N +" in block and "* 86400" in block, (
        "the coverage window must be derived from the candidate count, so it tracks a fleet "
        "that grows"
    )
    assert "$DRILL_CANDIDATES" in block, (
        "coverage must read the candidate list the drill publishes, not a second definition "
        "of eligibility free to drift from the one that selects"
    )


def test_check_eight_graces_each_candidate_from_when_it_joined() -> None:
    """A rotation-wide start date judges a volume added yesterday against a cycle it never had.

    Once the rotation itself is older than a cycle, every volume that later joins the backup set
    would be flagged immediately and page for a full cycle. The attempt stamp cannot stand in
    either: it is refreshed each time the volume comes up, so for anything in steady rotation it
    is never older than one cycle and the grace would never expire. Only a write-once join marker
    answers "has this volume had a fair chance".
    """
    block = _check_seven()
    assert "DRILL_SEEN" in block, (
        "coverage must grace each candidate from its own join marker"
    )
    assert "NOW_S - SEEN_AT > COVERAGE_MAX_S" in block, (
        "the grace period must be measured per candidate, from when it joined the rotation"
    )
    assert "ROTATION_START" not in block, (
        "a rotation-wide start date pages for a full cycle on every newly added volume"
    )
    code = _code(DRILL)
    assert '! -e "${SEEN_DIR}/${cand}"' in code, (
        "the join marker must be written once and never refreshed, or it decays into a second "
        "attempt stamp and the grace never expires"
    )


def test_check_eight_names_the_unproven_volumes() -> None:
    """A red that names no volume is unactionable and stops being read.

    That is the failure recorded on 2026-08-15, when a blank backup target made the backup
    plane's only monitor red for 9h with nothing an operator could act on.
    """
    block = _check_seven()
    assert "${UNPROVEN}" in block, "the coverage failure must name the volumes"
    assert "add 3" in block.split("UNPROVEN=")[-1], (
        "coverage pages below backup-failure severity, like check 7"
    )
