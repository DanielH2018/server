#!/usr/bin/env python3
"""Tests for longhorn_reap_lib/selection.py, the migrated-chain and seeds selections.

Each floor gets an `..._is_clean` / `..._is_flagged` pair per CLAUDE.md's red-proof rule: one
input it must keep, one it must delete. The population these two selectors read is the one
`classify_backups` deliberately drops — Completed backups with no RecurringJob label — so none
of these cases is reachable through `test_longhorn_reap_logic.py`'s fixtures.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_selectors.py
"""

from longhorn_reap_lib import logic
from longhorn_reap_lib import selection as selectors
from _reap_entrypoint_harness import _backup, _volume


def _claimed(name, vol, created, job, pvc, state="Completed"):
    """A Backup CR carrying the KubernetesStatus label Longhorn writes, as compact JSON."""
    return _backup(name, vol, created, job, state=state, pvc=pvc)


def test_backup_claim_reads_the_pvc_out_of_the_kubernetesstatus_label():
    assert (
        selectors.backup_claim(
            _claimed("old-0", "pvc-gone", "2026-08-14T00:00:00Z", "", "sonarr-config")
        )
        == "sonarr-config"
    )


def test_backup_claim_is_empty_when_the_label_is_not_parseable_json():
    backup = _backup("old-0", "pvc-gone", "2026-08-14T00:00:00Z", "")
    backup["status"]["labels"]["KubernetesStatus"] = "{not json"
    assert selectors.backup_claim(backup) == ""


def test_migrated_chain_floor_is_flagged_when_the_replacement_has_no_completed_backup():
    """The old chain is the only copy until the replacement has been backed up."""
    chain = selectors.select_migrated_chain(
        [
            _claimed(
                "old-0",
                "pvc-gone",
                "2026-08-14T00:00:00Z",
                "weekly-backup-d6",
                "sonarr-config",
            ),
            _claimed(
                "new-err",
                "pvc-new",
                "2026-08-20T00:00:00Z",
                "weekly-backup-d6",
                "sonarr-config",
                state="Error",
            ),
        ],
        {"pvc-new"},
        claim="sonarr-config",
        current_volume="pvc-new",
    )
    assert [name for name, *_ in chain.stranded] == ["old-0"]
    assert chain.current == []
    assert "no Completed backup" in (chain.refusal or "")


def test_migrated_chain_floor_is_clean_when_the_replacement_has_one():
    chain = selectors.select_migrated_chain(
        [
            _claimed("old-0", "pvc-gone", "2026-08-14T00:00:00Z", "", "sonarr-config"),
            _claimed("new-ok", "pvc-new", "2026-08-20T00:00:00Z", "", "sonarr-config"),
        ],
        {"pvc-new"},
        claim="sonarr-config",
        current_volume="pvc-new",
    )
    assert chain.refusal is None
    assert [name for name, *_ in chain.stranded] == ["old-0"]
    assert [name for name, *_ in chain.current] == ["new-ok"]


def test_migrated_chain_refuses_a_volume_list_without_the_claims_own_volume():
    """An empty or mis-parsed list makes every backup stranded and deletes a live chain."""
    chain = selectors.select_migrated_chain(
        [_claimed("live-0", "pvc-new", "2026-08-20T00:00:00Z", "", "sonarr-config")],
        set(),
        claim="sonarr-config",
        current_volume="pvc-new",
    )
    assert "does not contain sonarr-config's own volume" in (chain.refusal or "")


def test_migrated_chain_takes_only_this_claims_backups():
    """The PVC is compared as a parsed field, so `n8n-data` cannot select `n8n-data-something`."""
    chain = selectors.select_migrated_chain(
        [
            _claimed("mine", "pvc-gone", "2026-08-14T00:00:00Z", "", "n8n-data"),
            _claimed(
                "longer", "pvc-gone-2", "2026-08-14T00:00:00Z", "", "n8n-data-something"
            ),
            _claimed("live", "pvc-new", "2026-08-20T00:00:00Z", "", "n8n-data"),
            _claimed(
                "errored",
                "pvc-gone",
                "2026-08-13T00:00:00Z",
                "",
                "n8n-data",
                state="Error",
            ),
        ],
        {"pvc-new"},
        claim="n8n-data",
        current_volume="pvc-new",
    )
    assert [name for name, *_ in chain.stranded] == ["mine"]


def test_migrated_chain_takes_the_unlabelled_strand_classify_backups_cannot_see():
    """The discriminating case: a stranded chain's members need not carry a job label.

    `classify_backups` works on the LABELLED half of the Completed set, so deriving this mode
    from its `.orphaned` bucket would narrow the selection while reading as a refactor — the
    same shape as a derivation that drops a selector. A seed left on a migrated volume is
    exactly this: no job label, volume gone, and the migrated-chain mode's to delete.
    """
    backups = [
        _claimed(
            "seed-pvc-gone", "pvc-gone", "2026-08-10T00:00:00Z", "", "sonarr-config"
        ),
        _claimed(
            "old-0",
            "pvc-gone",
            "2026-08-14T00:00:00Z",
            "weekly-backup-d6",
            "sonarr-config",
        ),
        _claimed(
            "new-ok",
            "pvc-new",
            "2026-08-20T00:00:00Z",
            "weekly-backup-d6",
            "sonarr-config",
        ),
    ]
    chain = selectors.select_migrated_chain(
        backups, {"pvc-new"}, claim="sonarr-config", current_volume="pvc-new"
    )
    assert sorted(name for name, *_ in chain.stranded) == ["old-0", "seed-pvc-gone"]

    classified = logic.classify_backups(
        backups,
        logic.backup_owner_map([_volume("pvc-new", group="weekly-backup-d6")]),
        {"pvc-new"},
    )
    assert [name for name, *_ in classified.orphaned] == ["old-0"]


def test_seeds_floor_is_clean_when_the_volume_is_below_the_rotation_floor():
    """One shard run is not coverage: the seed is still that volume's only other copy."""
    seeds = selectors.select_seeds(
        [
            _claimed(
                "seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config"
            ),
            _backup("rotation-1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
        ],
        {"pvc-a"},
    )
    assert seeds.superseded == []
    reasons = {name: reason for name, _vol, reason in seeds.kept}
    assert "below the floor of 2" in reasons["seed-pvc-a"]


def test_seeds_floor_is_flagged_when_the_rotation_reaches_it():
    seeds = selectors.select_seeds(
        [
            _claimed(
                "seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config"
            ),
            _backup("rotation-1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
            _backup("rotation-2", "pvc-a", "2026-08-13T00:00:00Z", "weekly-backup-d2"),
        ],
        {"pvc-a"},
    )
    assert [name for name, *_ in seeds.superseded] == ["seed-pvc-a"]
    # A raised floor keeps the same seed: the floor is the knob, not the selection.
    assert (
        selectors.select_seeds(
            [
                _claimed(
                    "seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config"
                ),
                _backup(
                    "rotation-1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"
                ),
                _backup(
                    "rotation-2", "pvc-a", "2026-08-13T00:00:00Z", "weekly-backup-d2"
                ),
            ],
            {"pvc-a"},
            floor=3,
        ).superseded
        == []
    )


def test_a_seed_needs_both_markers_the_name_and_the_absent_job_label():
    """Either marker alone matches something that is not a seed."""
    seeds = selectors.select_seeds(
        [
            _claimed(
                "seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config"
            ),
            _backup(
                "seed-labelled", "pvc-a", "2026-08-11T00:00:00Z", "weekly-backup-d2"
            ),
            # The live store's wg-easy backup-7e481e73 has this shape: no job label, not a seed.
            _backup("backup-unlabelled", "pvc-a", "2026-08-12T00:00:00Z", ""),
            _backup("rotation-1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
            _backup("rotation-2", "pvc-a", "2026-08-13T00:00:00Z", "weekly-backup-d2"),
        ],
        {"pvc-a"},
    )
    assert [name for name, *_ in seeds.superseded] == ["seed-pvc-a"]


def test_a_seed_on_a_deleted_volume_is_left_to_the_migrated_chain_mode():
    seeds = selectors.select_seeds(
        [
            _claimed(
                "seed-pvc-gone", "pvc-gone", "2026-08-10T00:00:00Z", "", "sonarr-config"
            )
        ],
        {"pvc-a"},
    )
    assert seeds.superseded == []
    reasons = {name: reason for name, _vol, reason in seeds.kept}
    assert "migrated-chain" in reasons["seed-pvc-gone"]


def test_seeds_refuses_an_empty_volume_list():
    seeds = selectors.select_seeds(
        [_claimed("seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config")],
        set(),
    )
    assert "volume list is empty" in (seeds.refusal or "")
    assert seeds.superseded == []


def test_a_claim_narrows_the_seed_selection_to_one_pvc():
    """How the Class C spend is paced when the whole set does not fit in a day."""
    backups = [
        _claimed("seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config"),
        _claimed("seed-pvc-b", "pvc-b", "2026-08-10T00:00:00Z", "", "sonarr-config"),
        _backup("rot-a1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
        _backup("rot-a2", "pvc-a", "2026-08-19T00:00:00Z", "weekly-backup-d2"),
        _backup("rot-b1", "pvc-b", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
        _backup("rot-b2", "pvc-b", "2026-08-19T00:00:00Z", "weekly-backup-d3"),
    ]
    live = {"pvc-a", "pvc-b"}
    assert sorted(
        name for name, *_ in selectors.select_seeds(backups, live).superseded
    ) == ["seed-pvc-a", "seed-pvc-b"]
    assert [
        name
        for name, *_ in selectors.select_seeds(
            backups, live, claim="valheim-config"
        ).superseded
    ] == ["seed-pvc-a"]


def test_a_claim_no_backup_records_is_refused_not_narrowed_to_nothing():
    """A volume name passed where a claim name belongs must not read as "already retired"."""
    backups = [
        _claimed("seed-pvc-a", "pvc-a", "2026-08-10T00:00:00Z", "", "valheim-config"),
        _backup("rot-a1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
        _backup("rot-a2", "pvc-a", "2026-08-19T00:00:00Z", "weekly-backup-d2"),
    ]
    seeds = selectors.select_seeds(backups, {"pvc-a"}, claim="pvc-a")
    assert seeds.superseded == []
    assert "no Completed backup records the PVC pvc-a" in (seeds.refusal or "")


def test_claim_volume_resolves_the_pvcs_current_volume():
    volume, err = selectors.claim_volume(
        [
            {"metadata": {"name": "other"}, "spec": {"volumeName": "pvc-other"}},
            {"metadata": {"name": "sonarr-config"}, "spec": {"volumeName": "pvc-new"}},
        ],
        "sonarr-config",
        "homelab",
    )
    assert (volume, err) == ("pvc-new", "")


def test_claim_volume_refuses_a_claim_that_does_not_exist():
    volume, err = selectors.claim_volume([], "sonarr-config", "homelab")
    assert volume == ""
    assert "no PVC named sonarr-config" in err


def test_claim_volume_refuses_an_unbound_claim_by_name():
    """An empty volumeName would otherwise read back as a broken volume list."""
    volume, err = selectors.claim_volume(
        [{"metadata": {"name": "sonarr-config"}, "spec": {}}],
        "sonarr-config",
        "homelab",
    )
    assert volume == ""
    assert "bound to no volume" in err
