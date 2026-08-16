#!/usr/bin/env python3
"""Guards on the orphaned-backup reaper, whose only safety floor silently stopped working.

The reaper deletes Longhorn Backup objects stranded by a tier move, and its whole reason to
exist is FLOOR 1: never delete a volume's last recovery point. On 2026-08-16 that floor was
inoperative from the day it shipped, and nothing said so — the dry run reported `0 reapable`,
which reads as "nothing to do" and was in fact the bug's own output.

The mechanism is worth encoding rather than remembering. Ownership was read with
`-o jsonpath='{range .metadata.labels}{@}{" "}{end}'`, but ranging a MAP in kubectl jsonpath does
not iterate key/value pairs — it emits the whole label object as one space-free JSON blob. The
prefix match therefore never fired and the ownership map was empty for every volume. That does
not fail closed: the `$JOB == ${OWNER[$VOL]:-}` test then matches any backup with no
RecurringJob label, so a single hand-triggered probe backup counts as proof the volume's current
tier is producing backups, and FLOOR 1 (which fires only at a count of zero) stands down. On
wg-easy-config that would have deleted 3 of its 5 backups while its tier had produced none.

Run: uv run pytest ansible/tests/test_longhorn_reap_guard.py
"""

import re
from pathlib import Path

ANSIBLE = Path(__file__).resolve().parents[1]
K3S_TEMPLATES = ANSIBLE / "roles" / "setup" / "k3s" / "templates"
REAPER = K3S_TEMPLATES / "longhorn-reap-orphan-backups.sh.j2"

# `{range .metadata.labels}` and friends. Ranging .items[*] is fine and ubiquitous — that IS a
# list. This matches ranging into a map-valued field, which is the defect.
MAP_RANGE = re.compile(r"\{range\s+\.(metadata|status)\.(labels|annotations)\}")


def _shell_templates() -> list[Path]:
    return sorted(K3S_TEMPLATES.glob("*.sh.j2"))


def _code(path: Path) -> str:
    """The script minus its comments.

    The reaper documents the broken idiom verbatim so the next reader knows why it is not used;
    scanning comments too would make that explanation fail the guard that exists because of it.
    """
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def test_no_shell_template_ranges_a_label_map_in_jsonpath():
    """The bug class, not just the one instance.

    Asserted across every k3s shell template because the reaper and the backup-health script
    read the same labels for the same purpose, and the next script to need a volume's group
    will reach for the same idiom. The working form is a label selector (`-l group=enabled`)
    or `-o json` piped through jq reading `.metadata.labels | keys[]`.
    """
    offenders = [p.name for p in _shell_templates() if MAP_RANGE.search(_code(p))]
    assert not offenders, (
        "kubectl jsonpath cannot iterate a label map — it emits the whole object as one "
        f"token, so a prefix match over it silently matches nothing: {offenders}"
    )


def test_reaper_aborts_when_ownership_resolves_empty():
    """An empty ownership map must stop the run, not quietly disarm the floors.

    This is the assertion that would have caught the original defect: the lookup can break in
    ways this test cannot anticipate, so the script has to notice the *result* is unusable.
    """
    body = REAPER.read_text()
    assert "OWNER_COUNT" in body, (
        "the reaper must count resolved owners to be able to check it"
    )
    assert re.search(r"VOLUME_COUNT\s*>\s*0\s*&&\s*OWNER_COUNT\s*==\s*0", body), (
        "the reaper must refuse to run when volumes exist but none resolves to a "
        "recurring-job group — every floor depends on that map being populated"
    )
    assert re.search(r"ABORT:.*\n.*\n\s*exit 1", body), (
        "the empty-ownership guard must exit non-zero, not merely warn"
    )


def test_reaper_never_treats_an_unlabelled_backup_as_tier_evidence():
    """A hand-triggered backup carries no RecurringJob label and is neither current nor stranded.

    Both loops must skip it explicitly. Without this the empty-string comparison makes it stand
    in as proof that the volume's tier is healthy, which is precisely how FLOOR 1 was disarmed.
    """
    body = REAPER.read_text()
    skips = len(re.findall(r'\[\[\s*-z\s+"\$JOB"\s*\]\]\s*&&\s*continue', body))
    assert skips == 2, (
        "both the counting pass and the candidate pass must skip unlabelled backups; "
        f"found {skips} skip(s)"
    )


def test_reaper_does_not_delete_under_the_readonly_kubeconfig():
    """--apply used to run as homelab-readonly, so every delete was Forbidden.

    With no `set -e` and no return-code check, the loop ran to completion and the script exited
    0 after printing "deleting N object(s)" — the refusal and the success line arrived together.
    That is the recorded readonly-SA-reads-as-success shape, and it is dangerous here for the
    opposite of the obvious reason: the refusal was the only thing preventing data loss while
    the floor was broken, so "make the deletes work" is a change that must never land alone.
    """
    body = REAPER.read_text()
    assert "/etc/rancher/k3s/k3s.yaml" in body, (
        "--apply must use the admin kubeconfig; the read-only one cannot delete"
    )
    assert re.search(r"if\s*\(\(\s*NEEDS_ADMIN\s*==\s*1\s*\)\)", body), (
        "the admin kubeconfig must be selected only when a delete flag is set, so a dry run "
        "stays read-only and cannot delete anything even by accident"
    )
    assert re.search(
        r"NEEDS_ADMIN=\$\(\(\s*APPLY\s*\|\s*APPLY_DELETED_VOLUMES\s*\)\)", body
    ), "every flag that deletes must require the admin kubeconfig, not just --apply"
    assert re.search(r"if\s*!\s*\$KUBECTL delete", body), (
        "each delete's return code must be checked, or a Forbidden run reports success"
    )


def test_deleted_volume_strays_need_their_own_flag():
    """A backup whose volume is gone is reaped only under --apply-deleted-volumes.

    It is genuinely dead weight — the volume can never come back, so no floor will ever release
    it — but a deleted PVC is also exactly when someone reaches for a restore. Keeping it off
    --apply means the routine cleanup can never take the one thing a recovery would need.
    """
    body = REAPER.read_text()
    assert "--apply-deleted-volumes" in body, (
        "the deleted-volume bucket needs its own flag"
    )
    assert re.search(r"DELETED_VOLUME_STRAYS", body), (
        "deleted-volume strays must be collected into their own bucket, not CANDIDATES"
    )
    assert re.search(
        r"APPLY_DELETED_VOLUMES\s*==\s*1\s*\)\)\s*&&\s*delete_bucket\s+\"orphaned", body
    ), "the orphan bucket must be deleted only under its own flag, never under --apply"
