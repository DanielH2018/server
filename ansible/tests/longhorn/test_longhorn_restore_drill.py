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

Run: uv run pytest ansible/tests/longhorn/test_longhorn_restore_drill.py
"""

import re
from pathlib import Path

from _helpers import load_yaml
from _k8s_render import render_role_template
from _restore_drill import DRILL_DEFAULTS, DRILL_TEMPLATE
from _shell_render import rendered_shell_text
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, K3S_ROLE


DRILL = DRILL_TEMPLATE
CRONS = K3S_ROLE / "tasks" / "health-crons.yml"

from longhorn_lib import longhorn_backup_health_logic as logic  # noqa: E402

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
CRONJOB = ("longhorn-restore-drill", "cronjob.yaml.j2")
RBAC = ("longhorn-restore-drill", "rbac.yaml.j2")


# The drill as the host RUNS it, minus the comments that discuss the rejected idioms on purpose:
# scanning those would match an explanation as readily as a violation. Rendered rather than read,
# because a jq filter or a path built from a role default reads as `{{ ... }}` in the template and
# a pattern that matches nothing passes (#3186).
CODE = "\n".join(
    line
    for line in rendered_shell_text(*DRILL).splitlines()
    if not line.lstrip().startswith("#")
)


def test_drill_resolves_the_backup_instead_of_pinning_one() -> None:
    """A pinned backup ID dies the day retention deletes it. See the module docstring."""
    assert "get backups.longhorn.io" in CODE and "sort_by" in CODE, (
        "the drill must resolve the newest Completed backup at run time"
    )
    assert not re.search(r"backup-[0-9a-f]{16}", CODE), (
        "a literal backup ID is pinned; retention will delete it and the drill will then "
        "fail forever while reporting a restore failure"
    )


def test_drill_resolves_volume_and_size_from_the_cluster() -> None:
    """The one-off hardcoded both. A resized volume would silently restore into the wrong size."""
    assert "vol: .metadata.name" in CODE, "resolve the volume from the live Volume list"
    assert "size: (.spec.size | tostring)" in CODE, (
        "resolve the size from the live Volume"
    )
    assert not re.search(r"\bpvc-[0-9a-f]{8}-", CODE), (
        "a literal Longhorn volume name is pinned"
    )
    assert "134217728" not in CODE, "a literal size is pinned"


def test_drill_rotates_over_the_declared_backup_set() -> None:
    """Coverage is the point of rotating: one volume proves one volume, not the fleet.

    Eligibility must come from the `recurring-job-group.longhorn.io/*` LABEL — the same selector
    check 4 uses, and the only one that cannot drift from what the RecurringJobs really select. A
    PVC's storageClassName is immutable and still reads `longhorn` on volumes dropped from the
    backup set on 2026-08-08, so filtering by class would drill volumes nothing backs up.
    """
    defaults = load_yaml(DRILL_DEFAULTS)
    assert defaults["k3s_longhorn_restore_drill_pvc"] == "", (
        "the drill is pinned to one volume — rotation is disabled and 24 volumes go unproven"
    )
    assert re.search(r'startswith\("recurring-job-group\.longhorn\.io/"\)', CODE), (
        "eligibility must be selected by the recurring-job-group label"
    )
    assert 'select(. != "no-backup")' in CODE, (
        "the no-backup group is an explicit opt-out and must be excluded"
    )
    assert "storageClassName" not in CODE.split("cat >")[0], (
        "eligibility is filtered by storage class, which is immutable and drifts"
    )


def test_rotation_selects_by_attempt_not_by_success() -> None:
    """Selecting on last SUCCESS lets one broken volume starve the other 24.

    A volume that fails every drill would stay the least-recently-succeeded forever, be chosen
    every night, and the rotation would never advance — turning a one-volume fault into total
    loss of coverage. Stamping the attempt before the restore is what bounds a failing volume to
    one slot per cycle.
    """
    assert 'at=$(stamp_epoch "attempts.${pvc}")' in CODE, (
        "rotation must order candidates by their attempt stamp"
    )
    attempt_at = CODE.index('"attempts.${PVC}" "$ATTEMPT_AT"')
    assert attempt_at < CODE.index("$KUBECTL apply -f"), (
        "the attempt stamp must be written BEFORE the restore, or a failing volume starves "
        "the rotation"
    )
    assert CODE.index('"success.${PVC}" "$PASSED_AT"') > CODE.index(
        '"$BYTES" -ge "$MIN_BYTES"'
    ), "the per-volume success stamp must be written only after the assertions pass"


def test_drill_carries_the_source_block_size() -> None:
    """Omitted, the restore volume takes the GLOBAL block size, which is 16 MiB since 2026-08-19.

    The four R2 volumes are still 2 MiB and only convert on recreation. Longhorn refuses a volume
    size that is not a multiple of its block size, so a mismatch strands the restore on exactly
    the volumes rotation added.
    """
    assert 'backupBlockSize: "${BLOCKSIZE}"' in CODE, (
        "the restore volume must carry the source volume's block size"
    )


def test_drill_publishes_its_candidate_list() -> None:
    """Check 8 has no cluster credential worth the name, and a second definition would drift."""
    assert 'stamp_set candidates "$CANDIDATE_LIST"' in CODE, (
        "the drill must publish the eligible set for the heartbeat's coverage check"
    )


def test_drill_verification_is_not_tied_to_one_volume() -> None:
    """A filename-specific check cannot be repointed, which is how the one-off got stuck."""
    assert "acme.json" not in CODE, (
        "a volume-specific filename is baked in; the drill must work for whichever PVC "
        "k3s_longhorn_restore_drill_pvc names"
    )
    assert "files=" in CODE and "bytes=" in CODE, (
        "verification must count files and bytes, so it holds for any volume"
    )


def test_drill_tears_down_on_every_exit_path() -> None:
    """A half-restored volume left attached blocks the next run and occupies real disk."""
    assert "trap cleanup EXIT" in CODE, (
        "cleanup must run via a trap, not only on the success path — the drill exits early "
        "on every resolution failure"
    )
    for obj in ("delete pod", "delete pvc", "delete pv", "delete volume"):
        assert obj in CODE, f"cleanup must remove the drill's {obj.split()[-1]}"


def test_drill_stamps_only_after_the_assertions_pass() -> None:
    """The stamp IS the proof check 7 reads. Stamping early makes a broken drill read healthy."""
    assert CODE.count('stamp_set last-success "$PASSED_AT"') == 1, (
        "the stamp must be written in exactly one place"
    )
    stamp_at = CODE.index('stamp_set last-success "$PASSED_AT"')
    last_assert = max(CODE.index('"$FILES" -gt 0'), CODE.index('"$BYTES" -ge'))
    assert stamp_at > last_assert, (
        "the stamp is written before the data assertions, so a restored-but-empty volume "
        "would record a successful drill"
    )


def test_drill_rejects_an_empty_restore() -> None:
    """An empty filesystem mounts perfectly well and passes any mount-only check."""
    assert '"$FILES" -gt 0' in CODE, "a restore producing no files must fail"
    assert "MIN_BYTES" in CODE, (
        "a byte floor must guard against a mounted-but-empty volume"
    )


def test_drill_never_prints_the_restored_file() -> None:
    """These are service config volumes and several hold credentials — count, never print."""
    assert "wc -c" in CODE, "the probe must measure bytes, not emit them"
    assert "cat /drill" not in CODE, "the probe must never print a restored file"


def test_drill_distinguishes_no_output_from_an_empty_volume() -> None:
    """The first real run failed here, and the message could not say why.

    `kubectl run --rm -i` returned an EMPTY string on 2026-08-19 — no pod output and no error —
    so the drill reported "restored volume has no files:" with nothing after the colon. An empty
    capture and an empty volume are the two things this check exists to tell apart, and the
    attach form collapses them. The pod is now created, waited for, and read via logs.
    """
    assert "--rm -i" not in CODE, (
        "the attach form silently returns an empty capture; create the pod and read its logs"
    )
    assert "logs" in CODE and ".status.phase" in CODE, (
        "the probe must reach a terminal phase and be read from logs, so a failure is legible"
    )
    assert "produced no output" in CODE, (
        "an empty capture must fail with its own message, never as 'no files'"
    )


def test_drill_reports_a_failed_probe_pod_distinctly() -> None:
    """A pod that crashed says nothing about the volume — do not blame the data for it."""
    assert 'fail "probe pod ${PHASE}' in CODE, (
        "a non-Succeeded probe must report the phase"
    )


def test_the_drill_writes_no_host_file_for_the_checker_to_read() -> None:
    """Writer and reader are different users, and a host file made them disagree once.

    The drill runs as root; the heartbeat runs as sys_user. Root's umask on daniel-box is 027,
    so the old stamp directory was unreadable by the checker, and the first GREEN drill paged
    "no restore drill has ever succeeded" (2026-08-19). The state is a ConfigMap the checker
    reads through its own kubeconfig, so no file mode is left to get wrong.
    """
    assert "touch " not in CODE and "mkdir " not in CODE and "chmod " not in CODE, (
        "the drill is writing a host file again, and the checker's user may not be able to read it"
    )


def test_reader_is_installed_before_the_shim_that_invokes_it() -> None:
    """The shim's cron fires every 10 minutes and calls the reader by absolute path immediately.

    If the shim's release install ran first, a cron tick landing in the window between that task
    and the reader's `copy:` tasks below would call a reader that does not exist yet on a fresh
    install — a spurious page, not a real one (2026-09-04 review finding #5).
    """
    tasks = _tasks()
    names = [t.get("name") for t in tasks]
    reader_scripts_idx = names.index(
        "Install the Longhorn backup health reader scripts"
    )
    shim_release_idx = names.index(
        "Deploy the backup-health scripts as a versioned release"
    )
    assert reader_scripts_idx < shim_release_idx, (
        "the reader's files must land before the shim that invokes them by absolute path"
    )


def test_check_seven_fails_closed_when_the_drill_never_ran() -> None:
    """The never-run state is the one most in need of reporting. See the module docstring."""
    problem = logic.check_restore_drill(
        None, "/var/lib/longhorn-restore-drill/last-success", NOW, 3 * 86400, 3
    )
    assert problem is not None, "a missing stamp must page, not be skipped"
    assert problem[1].startswith("no restore drill has ever succeeded"), (
        "a never-run drill must page, not read green"
    )


def test_check_seven_rejects_an_unparseable_stamp() -> None:
    """A truncated or corrupt stamp is not evidence of a recent restore."""
    missing = logic.check_restore_drill(None, "/stamp", NOW, 3 * 86400, 3)
    garbage = logic.check_restore_drill("not-a-number", "/stamp", NOW, 3 * 86400, 3)
    stale = logic.check_restore_drill(
        str(int(NOW - 5 * 86400)), "/stamp", NOW, 3 * 86400, 3
    )
    assert garbage is not None, (
        "the stamp contents must be validated before being compared"
    )
    assert missing is not None and stale is not None
    assert len({missing[1], garbage[1], stale[1]}) == 3, (
        "missing, unparseable and stale stamps are three distinct failures and each must page "
        "with its own message"
    )


def test_check_seven_distinguishes_a_missing_stamp_from_an_unreadable_one() -> None:
    """Regression for the 2026-08-19 incident this module's docstring documents.

    root's umask made a FRESH, successful drill's stamp unreadable by the checker's own user,
    and folding
    that into "never ran" paged a lie — permanently, and precisely backwards — since a drill
    had just succeeded. "Never ran" and "ran, but the checker can't prove it" must stay two
    different messages.
    """
    missing = logic.check_restore_drill(None, "/stamp", NOW, 3 * 86400, 3)
    unreadable = logic.check_restore_drill(
        None, "/stamp", NOW, 3 * 86400, 3, stamp_unreadable=True
    )
    assert missing is not None and unreadable is not None
    assert missing[1] != unreadable[1]
    assert missing[1].startswith("no restore drill has ever succeeded")
    assert "could not be read" in unreadable[1]


def test_check_seven_pages_below_backup_failure_severity() -> None:
    """A stale drill is an assurance gap, not an active backup failure — rank 3, not 2."""
    for content in (None, "garbage", str(int(NOW - 5 * 86400))):
        problem = logic.check_restore_drill(content, "/stamp", NOW, 3 * 86400, 3)
        assert problem is not None and problem[0] == 3, (
            "rank 2 means 'a backup actively failed'; a stale drill would then outrank a real "
            "failure in the pushed message, which surfaces only the top-ranked problem"
        )


def _tasks():
    docs = load_yaml(CRONS)
    return docs if isinstance(docs, list) else []


def _release_bin_group_templates(group: str) -> list[str]:
    """The repo-relative template paths a release_bin group deploys, from the role defaults."""
    doc = load_yaml(K3S_DEFAULTS) or {}
    for value in doc.values() if isinstance(doc, dict) else []:
        if not isinstance(value, list):
            continue
        for entry in value:
            if isinstance(entry, dict) and entry.get("name") == group:
                return [str(t) for t in entry.get("templates") or []]
    return []


def _deploy_task_for(src_basename: str):
    """The task that deploys `src_basename`, in any of the three shapes health-crons.yml uses.

    A plain `src:`; a `loop:` of src/dest dicts (several same-shaped scripts folded into one
    `ansible.builtin.template`); and since the backup-health group became a versioned release,
    an `import_tasks` of release_bin.yml naming a GROUP whose file list lives in the role
    defaults. The third shape names no template in this file at all, so a resolver that only
    knew the first two returned None and the drill's guards below asserted on nothing.
    """
    for task in _tasks():
        tpl = task.get("ansible.builtin.template")
        if isinstance(tpl, dict):
            if tpl.get("src") == src_basename:
                return task
            for item in task.get("loop") or []:
                if isinstance(item, dict) and item.get("src") == src_basename:
                    return task
        target = str(task.get("ansible.builtin.import_tasks") or "")
        if target.endswith("release_bin.yml"):
            group = (task.get("vars") or {}).get("release_bin_group")
            if group and any(
                Path(t).name == src_basename
                for t in _release_bin_group_templates(group)
            ):
                return task
    return None


def test_drill_runs_as_its_own_serviceaccount_with_write_grants() -> None:
    """It creates and deletes cluster objects; the read-only SA is Forbidden on all of it."""
    cronjob = next(yaml_fast.safe_load_all(render_role_template(*CRONJOB)))
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "longhorn-restore-drill"
    verbs = {
        (rule["resources"][0], verb)
        for doc in yaml_fast.safe_load_all(render_role_template(*RBAC))
        if doc["kind"] in ("Role", "ClusterRole")
        for rule in doc["rules"]
        for verb in rule["verbs"]
    }
    assert ("volumes", "create") in verbs and (
        "persistentvolumes",
        "create",
    ) in verbs, (
        "the drill's ServiceAccount must be able to create the restore it proves"
    )


def test_the_host_cron_and_script_are_absent_arms_with_no_schedule() -> None:
    """The host copies are retired by absent arms, and an absent arm names no schedule key."""
    tasks = {t.get("name"): t for t in _tasks()}
    cron = tasks["Retire the host Longhorn restore drill cron"]["ansible.builtin.cron"]
    assert cron["state"] == "absent" and cron["cron_file"] == "longhorn-restore-drill"
    assert not {"minute", "hour", "day", "job"} & set(cron), (
        "an absent arm that names a schedule field reads a key that left this role's defaults"
    )
    script = tasks["Retire the host Longhorn restore drill script"][
        "ansible.builtin.file"
    ]
    assert script == {
        "path": "/usr/local/bin/longhorn-restore-drill.sh",
        "state": "absent",
    }


def test_the_host_no_longer_deploys_the_drill_script() -> None:
    """Check 7 reads the ConfigMap the CronJob writes, so no release group ships the script."""
    assert _deploy_task_for("longhorn-restore-drill.sh.j2") is None, (
        "a release group still deploys the host drill script that the CronJob replaced"
    )


def test_drill_runs_daily() -> None:
    """A monthly drill leaves a broken restore path undetected for weeks."""
    defaults = load_yaml(DRILL_DEFAULTS)
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
    defaults = load_yaml(K3S_DEFAULTS)
    max_age = defaults["k3s_longhorn_restore_drill_max_age_days"]
    assert 2 <= max_age <= 7, (
        f"max_age_days is {max_age}: a nightly drill should tolerate a couple of bad nights, "
        "not a month of silence"
    )


def test_check_eight_derives_its_window_from_the_fleet() -> None:
    """A hardcoded coverage window goes stale the moment a volume joins the backup set."""
    small = logic.check_restore_coverage(
        ["pvc-1"], {"pvc-1": NOW - 100 * 86400}, {}, NOW, 5
    )
    big = logic.check_restore_coverage(
        ["pvc-%d" % i for i in range(20)],
        {"pvc-1": NOW - 100 * 86400},
        {},
        NOW,
        5,
    )
    assert small is not None and big is not None
    assert "6d" in small[1] and "25d" in big[1], (
        "the coverage window must be derived from the candidate count (cand_n + slack days), "
        "so it tracks a fleet that grows rather than staying fixed"
    )


def test_check_eight_graces_each_candidate_from_when_it_joined() -> None:
    """A rotation-wide start date judges a volume added yesterday against a cycle it never had.

    Once the rotation itself is older than a cycle, every volume that later joins the backup set
    would be flagged immediately and page for a full cycle. The attempt stamp cannot stand in
    either: it is refreshed each time the volume comes up, so for anything in steady rotation it
    is never older than one cycle and the grace would never expire. Only a write-once join marker
    answers "has this volume had a fair chance".
    """
    coverage_days = 2 + 5  # cand_n=2, slack=5
    seen = {
        "pvc-old": NOW
        - (coverage_days + 1) * 86400,  # joined long ago, past its window
        "pvc-new": NOW - 86400,  # joined yesterday, well within its own window
    }
    problem = logic.check_restore_coverage(["pvc-old", "pvc-new"], seen, {}, NOW, 5)
    assert problem is not None
    assert "pvc-old" in problem[1] and "pvc-new" not in problem[1], (
        "the grace period must be measured per candidate, from when IT joined the rotation — "
        "a rotation-wide start date would page every newly added volume for a full cycle"
    )


def test_check_eight_names_the_unproven_volumes() -> None:
    """A red that names no volume is unactionable and stops being read.

    That is the failure recorded on 2026-08-15, when a blank backup target made the backup
    plane's only monitor red for 9h with nothing an operator could act on.
    """
    coverage_days = 1 + 5
    seen = {"pvc-1": NOW - (coverage_days + 1) * 86400}
    problem = logic.check_restore_coverage(["pvc-1"], seen, {}, NOW, 5)
    assert problem is not None
    assert "pvc-1" in problem[1], "the coverage failure must name the volumes"
    assert problem[0] == 3, "coverage pages below backup-failure severity, like check 7"
