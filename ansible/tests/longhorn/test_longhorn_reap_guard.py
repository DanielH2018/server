#!/usr/bin/env python3
"""Guards on the orphaned-backup reaper's safety floor.

The reaper deletes Longhorn Backup objects stranded by a tier move, and its whole reason to
exist is FLOOR 1: never delete a volume's last recovery point. A floor that silently stops
working reports `0 reapable` in a dry run, which reads as "nothing to do".

The mechanism is worth encoding rather than remembering. Reading ownership with
`-o jsonpath='{range .metadata.labels}{@}{" "}{end}'` fails: ranging a MAP in kubectl jsonpath
does not iterate key/value pairs — it emits the whole label object as one space-free JSON blob.
The prefix match therefore never fires and the ownership map is empty for every volume. That does
not fail closed: the `$JOB == ${OWNER[$VOL]:-}` test then matches any backup with no
RecurringJob label, so a single hand-triggered probe backup counts as proof the volume's current
tier is producing backups, and FLOOR 1 (which fires only at a count of zero) stands down. On
wg-easy-config that would have deleted 3 of its 5 backups while its tier had produced none.

The classification logic lives in scripts/backup/longhorn_reap_logic.py,
read with `kubectl -o json` rather than jsonpath, which closes the defect class this file
guards. The tests below exercise that module directly rather than regex-matching shell source:
a passing regex proves the RIGHT WORDS are present, never that the behaviour they describe
holds.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_reap_guard.py
"""

import re

from _shell_render import rendered_shell_texts

import longhorn_reap_logic as logic
from _reap_entrypoint_harness import _backup

# `{range .metadata.labels}` and friends. Ranging .items[*] is fine and ubiquitous — that IS a
# list. This matches ranging into a map-valued field, which is the defect.
MAP_RANGE = re.compile(r"\{range\s+\.(metadata|status)\.(labels|annotations)\}")


# The templates the jsonpath sweep below must reach. Named rather than counted: the (plane, role)
# filter reads empty the day a script moves, and an `all(...)` over nothing passes.
GUARDED_TEMPLATES = frozenset({"longhorn-backup-health.sh.j2"})


def _k3s_shell_scripts() -> dict[str, str]:
    """Every `setup/k3s` shell script as the host runs it, by template name.

    Rendered rather than read: a jsonpath expression assembled from a role default would reach
    bash whole and read as `{{ ... }}` in the source, which is a guard over nothing (#3186).
    """
    found = {
        name: text
        for plane, role, name, text in rendered_shell_texts()
        if (plane, role) == ("setup", "k3s")
    }
    missing = GUARDED_TEMPLATES - set(found)
    assert not missing, f"the setup/k3s render no longer holds: {sorted(missing)}"
    return found


def _code(text: str) -> str:
    """A script minus its comments.

    The reaper documents the broken idiom verbatim so the next reader knows why it is not used;
    scanning comments too would make that explanation fail the guard that exists because of it.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_no_shell_template_ranges_a_label_map_in_jsonpath():
    """The bug class, not just the one instance.

    Asserted across every k3s shell template because the reaper and the backup-health script
    read the same labels for the same purpose, and the next script to need a volume's group
    will reach for the same idiom. The working form is a label selector (`-l group=enabled`)
    or `-o json` piped through jq/json.loads reading `.metadata.labels | keys[]` — which is what
    longhorn_reap_logic.backup_owner_map / snapshot_owner_map do.
    """
    offenders = [
        name
        for name, text in _k3s_shell_scripts().items()
        if MAP_RANGE.search(_code(text))
    ]
    assert not offenders, (
        "kubectl jsonpath cannot iterate a label map — it emits the whole object as one "
        f"token, so a prefix match over it silently matches nothing: {offenders}"
    )


def test_reaper_aborts_when_ownership_resolves_empty():
    """An empty ownership map must stop the run, not quietly disarm the floors.

    The lookup can break in ways this test cannot anticipate, so the module has to notice the *result* is unusable
    rather than trust that an empty map means "nothing is stranded".
    """
    reason = logic.abort_reason(volume_count=22, owner_count=0)
    assert reason is not None
    assert "ABORT" in reason
    assert logic.abort_reason(volume_count=22, owner_count=22) is None


def test_reaper_never_treats_an_unlabelled_backup_as_tier_evidence():
    """A hand-triggered backup carries no RecurringJob label and is neither current nor stranded.

    Reproduces the wg-easy-config case from this module's docstring: without excluding the
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
    """--apply must not run as homelab-readonly, where every delete is Forbidden.

    Without a return-code check the loop runs to completion and the script exits 0 after printing
    "deleting N object(s)" — the refusal and the success line arrive together. That is the
    readonly-SA-reads-as-success shape, and it is dangerous here for the opposite of the obvious
    reason: with a broken floor the refusal is the only thing preventing data loss, so "make the
    deletes work" is a change that must never land alone.
    longhorn_reap_orphan_backups.py refuses before making any kubectl call at all when the admin
    kubeconfig is unreadable — proven directly, not by grepping for a path string.
    """
    path, err = logic.resolve_kubeconfig(
        needs_admin=True,
        admin_readable=False,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo .venv/bin/python -B scripts/backup/longhorn_reap_orphan_backups.py --apply",
    )
    assert path is None
    assert err is not None and "/etc/rancher/k3s/k3s.yaml" in err

    path, err = logic.resolve_kubeconfig(
        needs_admin=True,
        admin_readable=True,
        admin_path="/etc/rancher/k3s/k3s.yaml",
        readonly_path="/home/ubuntu/.kube/config",
        sudo_hint="sudo .venv/bin/python -B scripts/backup/longhorn_reap_orphan_backups.py --apply",
    )
    assert path == "/etc/rancher/k3s/k3s.yaml" and err is None


def test_deleted_volume_strays_need_their_own_flag():
    """A backup whose volume is gone is reaped only under --apply-deleted-volumes.

    It is genuinely dead weight — the volume can never come back, so no floor will ever release
    it — but a deleted PVC is also exactly when someone reaches for a restore. classify_backups
    keeps it out of `.candidates` (the plain --apply bucket) and puts it in `.orphaned`, which
    longhorn_reap_orphan_backups.py's main() only deletes when --apply-deleted-volumes is set —
    proven end-to-end in test_longhorn_reap_backups_cli.py
    ::test_backups_apply_deleted_volumes_only_deletes_the_orphaned_bucket.
    """
    result = logic.classify_backups(
        [_backup("stray", "gone-vol", "2026-08-14T00:00:00Z", "daily-backup")],
        owner={},
        # A live volume alongside the deleted one. An EMPTY set is the separate case
        # classify_backups refuses outright: it means the volume read returned nothing,
        # not that every volume was deleted, and orphaning the whole backup set on it is the
        # failure that refusal prevents.
        existing_volumes={"live-vol"},
    )
    assert result.candidates == []
    assert [n for n, *_ in result.orphaned] == ["stray"]
