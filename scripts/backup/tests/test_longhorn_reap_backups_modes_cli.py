#!/usr/bin/env python3
"""The backups reaper's two operator modes, run as real subprocesses.

Sibling of `test_longhorn_reap_backups_cli.py`, which covers the default strays mode. What these
pin is the I/O shell around `select_migrated_chain` and `select_seeds`: the PVC read the
migrated-chain mode makes, that a refused floor deletes nothing even in a dry run, and that the
cap and the delete transport are the ones the strays mode already uses. The selections themselves
are covered against fixtures in `test_longhorn_reap_selectors.py`.

The stub `k3s` and the staging harness are shared in `_reap_entrypoint_harness.py`.

Run: uv run pytest scripts/backup/tests/test_longhorn_reap_backups_modes_cli.py
"""

import pytest

from _reap_entrypoint_harness import (
    BACKUPS_ENTRY,
    _backup,
    _delete_names,
    _pvc,
    _run,
    _volume,
)


def _chain_fixtures(replacement_state="Completed"):
    """One claim with a stranded chain on a deleted volume and a replacement on a live one."""
    return {
        "volumes": [_volume("pvc-new", "weekly-backup-d6")],
        "persistentvolumeclaims": [_pvc("sonarr-config", "pvc-new")],
        "backups": [
            _backup(
                "old-0",
                "pvc-gone",
                "2026-08-14T00:00:00Z",
                "weekly-backup-d6",
                pvc="sonarr-config",
            ),
            _backup(
                "new-0",
                "pvc-new",
                "2026-08-20T00:00:00Z",
                "weekly-backup-d6",
                state=replacement_state,
                pvc="sonarr-config",
            ),
        ],
    }


def _seed_fixtures():
    """One seed whose volume's rotation holds the floor, and one whose rotation does not."""
    return {
        "volumes": [
            _volume("pvc-a", "weekly-backup-d2"),
            _volume("pvc-b", "weekly-backup-d3"),
        ],
        "backups": [
            _backup(
                "seed-pvc-a",
                "pvc-a",
                "2026-08-10T00:00:00Z",
                None,
                pvc="valheim-config",
            ),
            _backup("rot-a1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
            _backup("rot-a2", "pvc-a", "2026-08-19T00:00:00Z", "weekly-backup-d2"),
            _backup(
                "seed-pvc-b", "pvc-b", "2026-08-10T00:00:00Z", None, pvc="sonarr-config"
            ),
            _backup("rot-b1", "pvc-b", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
        ],
    }


def test_migrated_chain_dry_run_lists_the_chain_and_emits_no_delete(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "migrated-chain", "--claim", "sonarr-config"],
        _chain_fixtures(),
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "old-0 pvc-gone" in proc.stdout
    assert "dry run" in proc.stdout
    assert _delete_names(calls) == []


def test_migrated_chain_apply_deletes_exactly_the_stranded_chain(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode=migrated-chain", "--claim=sonarr-config", "--apply"],
        _chain_fixtures(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert [c for c in calls if "delete" in c] == [
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "delete",
            "backups.longhorn.io",
            "old-0",
            "--ignore-not-found",
            "--timeout=120s",
        ]
    ]


def test_migrated_chain_floor_refuses_in_a_dry_run_too(tmp_path):
    # The floor has to hold in a dry run, or the dry run reads as "this is safe to apply" for a
    # claim whose replacement has no recovery point of its own yet.
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "migrated-chain", "--claim", "sonarr-config"],
        _chain_fixtures(replacement_state="Error"),
        tmp_path,
    )
    assert proc.returncode == 1
    assert "no Completed backup" in proc.stderr
    assert _delete_names(calls) == []


def test_migrated_chain_refuses_a_claim_with_no_pvc_and_reads_no_backups(tmp_path):
    fixtures = _chain_fixtures()
    fixtures["persistentvolumeclaims"] = []
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "migrated-chain", "--claim", "sonarr-config"],
        fixtures,
        tmp_path,
    )
    assert proc.returncode == 1
    assert "no PVC named sonarr-config" in proc.stderr
    assert _delete_names(calls) == []


def test_seeds_apply_deletes_only_the_seed_its_rotation_superseded(tmp_path):
    # pvc-b holds one rotation backup, below the default floor of 2, so its seed stays.
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "seeds", "--apply"],
        _seed_fixtures(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_names(calls) == ["seed-pvc-a"]


def test_seeds_dry_run_emits_no_delete_and_names_both_seeds(tmp_path):
    proc, calls = _run(BACKUPS_ENTRY, ["--mode", "seeds"], _seed_fixtures(), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "seed-pvc-a" in proc.stdout and "seed-pvc-b" in proc.stdout
    assert _delete_names(calls) == []


def test_seeds_raised_floor_keeps_the_seed_the_default_floor_would_delete(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "seeds", "--seed-floor", "3", "--apply"],
        _seed_fixtures(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _delete_names(calls) == []


def test_seeds_claim_narrows_the_apply_to_one_pvc(tmp_path):
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "seeds", "--claim", "sonarr-config", "--apply"],
        _seed_fixtures(),
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 0, proc.stderr
    # sonarr-config's seed is the one BELOW the floor, so narrowing to it deletes nothing.
    assert _delete_names(calls) == []


def test_seeds_apply_refuses_over_the_deletion_cap(tmp_path):
    # The cap is one number for all three modes, calibrated on the strays mode's ~520 Class C
    # per deletion. A seeds sweep over it refuses before the first delete, the same as a strays
    # sweep does, and the operator re-decides against `probe.py b2-budget`.
    proc, calls = _run(
        BACKUPS_ENTRY,
        ["--mode", "seeds", "--apply", "--max-deletions", "1"],
        {
            "volumes": [
                _volume("pvc-a", "weekly-backup-d2"),
                _volume("pvc-b", "weekly-backup-d3"),
            ],
            "backups": [
                _backup(
                    "seed-pvc-a",
                    "pvc-a",
                    "2026-08-10T00:00:00Z",
                    None,
                    pvc="valheim-config",
                ),
                _backup("rot-a1", "pvc-a", "2026-08-20T00:00:00Z", "weekly-backup-d2"),
                _backup("rot-a2", "pvc-a", "2026-08-19T00:00:00Z", "weekly-backup-d2"),
                _backup(
                    "seed-pvc-b",
                    "pvc-b",
                    "2026-08-10T00:00:00Z",
                    None,
                    pvc="sonarr-config",
                ),
                _backup("rot-b1", "pvc-b", "2026-08-20T00:00:00Z", "weekly-backup-d3"),
                _backup("rot-b2", "pvc-b", "2026-08-19T00:00:00Z", "weekly-backup-d3"),
            ],
        },
        tmp_path,
        admin_readable=True,
    )
    assert proc.returncode == 1
    assert "--max-deletions cap of 1" in proc.stderr
    assert _delete_names(calls) == []


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--mode", "migrated-chain", "--claim", "sonarr-config"],
        ["--mode", "seeds"],
    ],
    ids=["strays", "migrated-chain", "seeds"],
)
def test_every_modes_dry_run_prints_the_per_block_cost(tmp_path, args):
    """The reaper's output is where the deletion cost now has to survive being read.

    It was the retired `prune_backups.yml` header for the two operator modes: a short reapable
    list reads as cheap, while the spend is ~1.28 Class C per stored block.
    """
    fixtures = _chain_fixtures() if "migrated-chain" in args else _seed_fixtures()
    proc, _calls = _run(BACKUPS_ENTRY, args, fixtures, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "1.28 Class C" in proc.stdout
    assert "b2-budget" in proc.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "bogus"],
        ["--mode", "migrated-chain"],
        ["--mode", "seeds", "--apply-deleted-volumes"],
        ["--claim", "sonarr-config"],
        ["--seed-floor", "3"],
        ["--mode"],
        ["--mode", "seeds", "--seed-floor", "0"],
    ],
    ids=[
        "unknown-mode",
        "migrated-chain-without-a-claim",
        "deleted-volumes-outside-the-strays-mode",
        "claim-in-the-strays-mode",
        "seed-floor-outside-the-seeds-mode",
        "mode-without-a-value",
        "seed-floor-below-one",
    ],
)
def test_a_flag_that_belongs_to_another_mode_is_rejected(tmp_path, args):
    """A mode flag silently ignored would read as a scope the run did not have."""
    proc, calls = _run(BACKUPS_ENTRY, args, {"volumes": []}, tmp_path)
    assert proc.returncode == 2, proc.stdout
    assert calls == []
