"""`probe.py b2-spend` and `probe.py b2-record`: the B2 transaction ledger.

B2 publishes no usage API, so backup spend is measured from Longhorn's own logs and
maintenance spend is recorded here, into a local ledger, so it is not reconstructed from
memory after the fact.
"""

import pytest

import probe_b2_ledger as ledger
import probe_core as core

SPEND_LOG = [
    (
        1,
        '[pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc-r-9d333575] time="..." '
        'msg="Created snapshot changed blocks: 104 mappings, 104 blocks and 75 new blocks"',
    ),
    (
        2,
        '[pvc-00d8210a-e38d-49f9-ba22-3aff333f59ab-r-b0d3cf84] time="..." '
        'msg="Created snapshot changed blocks: 77 mappings, 77 blocks and 67 new blocks"',
    ),
    (3, 'time="..." msg="Performing delta block backup"'),
]


def test_parse_duration_seconds_accepts_the_documented_forms():
    assert core.parse_duration_seconds("30m") == 1800
    assert core.parse_duration_seconds("6h") == 21600
    assert core.parse_duration_seconds("2d") == 172800
    assert core.parse_duration_seconds("1w") == 604800


def test_parse_duration_seconds_rejects_junk_rather_than_defaulting():
    """A silently-ignored duration would query Loki's one-hour default and report an empty
    window as 'nothing ran', which is the failure this flag exists to prevent."""
    for bad in ("6", "h", "6y", "-2d", "", "6 h"):
        with pytest.raises(SystemExit):
            core.parse_duration_seconds(bad)


def test_parse_backup_spend_counts_delta_blocks_per_volume():
    """`blocks` is the delta Longhorn walks, and it HeadObjects each one — so that count is the
    backup's Class B cost. `new blocks` is what it uploaded, which is Class A and free."""
    vols = ledger.parse_backup_spend(SPEND_LOG)
    assert vols["pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc"]["blocks"] == 104
    assert vols["pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc"]["new_blocks"] == 75
    assert vols["pvc-00d8210a-e38d-49f9-ba22-3aff333f59ab"]["backups"] == 1
    # The unrelated progress line must not be counted as a backup.
    assert len(vols) == 2


def test_parse_backup_spend_keeps_lines_whose_replica_prefix_was_trimmed():
    """Dropping an unattributable line would understate spend, and understating is the failure
    mode that matters — the cap does not care which volume it was."""
    vols = ledger.parse_backup_spend(
        [
            (
                1,
                'msg="Created snapshot changed blocks: 9 mappings, 9 blocks and 2 new blocks"',
            )
        ]
    )
    assert vols["unattributed"]["blocks"] == 9


def test_format_backup_spend_totals_and_says_when_the_window_was_empty():
    text = ledger.format_backup_spend(ledger.parse_backup_spend(SPEND_LOG), "6h")
    assert "backups over 6h: 181 Class B measured" in text
    empty = ledger.format_backup_spend({}, "6h")
    assert "no backups logged" in empty and "widen --since" in empty


def test_format_backup_spend_shows_maintenance_and_never_sums_the_two_windows():
    """Backups span --since; the ledger covers the UTC day. A combined total would match
    neither, so the report must keep them apart."""
    text = ledger.format_backup_spend(
        ledger.parse_backup_spend(SPEND_LOG),
        "6h",
        ledger={"drain": {"runs": 2, "class_a": 0, "class_b": 64, "class_c": 9}},
    )
    assert "backups over 6h: 181 Class B measured" in text
    assert "drain" in text and "64 Class B" in text
    assert "245" not in text  # 181 + 64 must not appear as a combined figure


def test_parse_b2_ledger_totals_per_tool_and_skips_malformed_lines():
    tools = ledger.parse_b2_ledger(
        [
            "2026-08-17T12:00:00Z\tdrain\t972\t59\t5\tretain 2",
            "2026-08-17T13:00:00Z\tdrain\t179\t5\t4\tradarr",
            "2026-08-17T14:00:00Z\tb2-budget\t0\t0\t5\t4 pages",
            "not a ledger line",
            "2026-08-17T15:00:00Z\tdrain\tnot\tnumbers\there\t",
        ]
    )
    assert tools["drain"] == {"runs": 2, "class_a": 1151, "class_b": 64, "class_c": 9}
    assert tools["b2-budget"]["class_c"] == 5
    assert "not a ledger line" not in tools


def test_record_b2_spend_never_raises_when_the_ledger_is_unwritable(monkeypatch):
    """A ledger failure must not fail the real work — the accounting is secondary to the
    operation it is accounting for."""
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", "/proc/cannot/create/this")
    ledger.record_b2_spend("drain", class_c=5)  # must not raise


def test_record_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", str(tmp_path))
    ledger.record_b2_spend("drain", class_a=100, class_b=59, class_c=5, note="retain 2")
    ledger.record_b2_spend("b2-budget", class_c=5)
    tools = ledger.read_b2_ledger()
    assert tools["drain"]["class_b"] == 59
    assert tools["b2-budget"]["class_c"] == 5
