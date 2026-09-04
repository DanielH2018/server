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

The classification logic that used to be inline bash string processing in
longhorn-reap-orphan-backups.sh.j2 now lives in longhorn_reap_logic.py
(ansible/roles/setup/k3s/files/), read with `kubectl -o json` rather than jsonpath — the port
that closes the defect class this file guards. The tests below exercise that module directly
rather than regex-matching shell source, which is what let the original floor ship broken:
a passing regex proved the RIGHT WORDS were present, never that the behaviour they described
actually held.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_reap_guard.py
"""

import re
import sys
from pathlib import Path

from _helpers import ANSIBLE

sys.path.insert(0, str(ANSIBLE / "roles" / "setup" / "k3s" / "files"))
import longhorn_reap_logic as logic

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


def _backup(name, vol, created, job, state="Completed"):
    return {
        "metadata": {"name": name},
        "status": {
            "volumeName": vol,
            "snapshotCreatedAt": created,
            "labels": {"RecurringJob": job} if job else {},
            "state": state,
        },
    }


def test_no_shell_template_ranges_a_label_map_in_jsonpath():
    """The bug class, not just the one instance.

    Asserted across every k3s shell template because the reaper and the backup-health script
    read the same labels for the same purpose, and the next script to need a volume's group
    will reach for the same idiom. The working form is a label selector (`-l group=enabled`)
    or `-o json` piped through jq/json.loads reading `.metadata.labels | keys[]` — which is what
    longhorn_reap_logic.backup_owner_map / snapshot_owner_map now do.
    """
    offenders = [p.name for p in _shell_templates() if MAP_RANGE.search(_code(p))]
    assert not offenders, (
        "kubectl jsonpath cannot iterate a label map — it emits the whole object as one "
        f"token, so a prefix match over it silently matches nothing: {offenders}"
    )


def test_reaper_shim_no_longer_touches_kubectl_at_all():
    """The thin shell shim carries no kubectl logic to get wrong.

    The whole reason FLOOR 1 could ship broken and unnoticed is that the ownership lookup lived
    in bash string processing with no test. The port's fix is structural: the shim forwards argv
    and two env vars, and every kubectl read/decision lives in longhorn_reap_logic.py +
    longhorn_reap_orphan_backups.py, both plain files with the tests below.
    """
    body = REAPER.read_text()
    assert "kubectl" not in body, (
        "the shim should forward to the Python entry point, not call kubectl itself: "
        + body
    )
    assert "longhorn_reap_orphan_backups.py" in body


def test_reaper_aborts_when_ownership_resolves_empty():
    """An empty ownership map must stop the run, not quietly disarm the floors.

    This is the assertion that would have caught the original defect: the lookup can break in
    ways this test cannot anticipate, so the module has to notice the *result* is unusable
    rather than trust that an empty map means "nothing is stranded".
    """
    assert logic.abort_reason(volume_count=22, owner_count=0) is not None
    reason = logic.abort_reason(volume_count=22, owner_count=0)
    assert "ABORT" in reason
    assert logic.abort_reason(volume_count=22, owner_count=22) is None


def test_reaper_never_treats_an_unlabelled_backup_as_tier_evidence():
    """A hand-triggered backup carries no RecurringJob label and is neither current nor stranded.

    Reproduces the wg-easy-config incident from this module's docstring: without excluding the
    unlabelled probe backup from the counting pass, it stands in as proof the volume's tier is
    healthy, which is precisely how FLOOR 1 was disarmed and would have deleted 3 of 5 backups.
    """
    owner = {"wg-easy-config": "weekly-backup-d3"}
    backups = [
        _backup("probe", "wg-easy-config", "2026-08-19T00:00:00Z", job=""),
        _backup("stray-2", "wg-easy-config", "2026-08-15T00:00:00Z", "daily-backup"),
        _backup("stray-1", "wg-easy-config", "2026-08-14T00:00:00Z", "daily-backup"),
    ]
    result = logic.classify_backups(backups, owner, existing_volumes={"wg-easy-config"})
    # Nothing reaped: the tier has produced no real backup, and the probe counts toward nothing.
    assert result.candidates == []
    assert "probe" not in {name for name, *_ in result.kept}


def test_reaper_does_not_delete_under_the_readonly_kubeconfig():
    """--apply used to run as homelab-readonly, so every delete was Forbidden.

    With no return-code check the loop ran to completion and the script exited 0 after printing
    "deleting N object(s)" — the refusal and the success line arrived together. That is the
    recorded readonly-SA-reads-as-success shape, and it is dangerous here for the opposite of
    the obvious reason: the refusal was the only thing preventing data loss while the floor was
    broken, so "make the deletes work" is a change that must never land alone.
    longhorn_reap_orphan_backups.py refuses before making any kubectl call at all when the admin
    kubeconfig is unreadable — proven directly, not by grepping for a path string.
    """
    path, err = logic.resolve_kubeconfig(
        needs_admin=True,
        admin_readable=False,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo /usr/local/bin/longhorn-reap-orphan-backups.sh --apply",
    )
    assert path is None
    assert err is not None and "/etc/rancher/k3s/k3s.yaml" in err

    path, err = logic.resolve_kubeconfig(
        needs_admin=True,
        admin_readable=True,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo /usr/local/bin/longhorn-reap-orphan-backups.sh --apply",
    )
    assert path == "/etc/rancher/k3s/k3s.yaml" and err is None


def test_deleted_volume_strays_need_their_own_flag():
    """A backup whose volume is gone is reaped only under --apply-deleted-volumes.

    It is genuinely dead weight — the volume can never come back, so no floor will ever release
    it — but a deleted PVC is also exactly when someone reaches for a restore. classify_backups
    keeps it out of `.candidates` (the plain --apply bucket) and puts it in `.orphaned`, which
    longhorn_reap_orphan_backups.py's main() only deletes when --apply-deleted-volumes is set —
    proven end-to-end in test_longhorn_reap_entrypoints.py
    ::test_backups_apply_deleted_volumes_only_deletes_the_orphaned_bucket.
    """
    result = logic.classify_backups(
        [_backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")],
        owner={},
        existing_volumes=set(),
    )
    assert result.candidates == []
    assert [n for n, *_ in result.orphaned] == ["stray"]
