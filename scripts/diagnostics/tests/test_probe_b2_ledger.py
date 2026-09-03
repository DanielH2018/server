"""`probe.py b2-spend` and `probe.py b2-record`: the B2 transaction ledger.

B2 publishes no usage API, so backup spend is measured from Longhorn's own logs and
maintenance spend is recorded here, into a local ledger, so it is not reconstructed from
memory after the fact.
"""

from datetime import datetime, timezone

import pytest

from diagnostics.probe_lib import b2_ledger as ledger
from diagnostics.probe_lib import core

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
    """Backups span --since; the ledger covers the UTC day.

    A combined total would match neither, so the report must keep them apart.
    """
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


# `probe.py b2-deletions`: charging a deletion that already happened. The two backup targets are
# real — `default` is B2, `r2` is Cloudflare R2 — and only the first is capped at 2,500 Class C a
# day, so the R2 line below is the input the filter must REJECT.
B2_TARGET = "s3://daniel-server-kopia@us-east-005/longhorn"
R2_TARGET = "s3://daniel-box@auto/longhorn"


def _delete_line(target, backup, volume, verb="Complete"):
    return (
        f'time="..." level=info msg="{verb} deleting backup {target}'
        f'?backup={backup}&volume={volume}" '
        'func="engineapi.(*BackupTargetClient).BackupDelete" file="backups.go:310"'
    )


VOL_A = "pvc-c2ca0afb-74f0-4507-a29a-3cf40aac175d"
VOL_B = "pvc-f7c223ec-c5fc-49d4-a0ab-c33f934dffbb"
DELETE_LOG = [
    (1, _delete_line(B2_TARGET, "backup-0ab05217dd5a4501", VOL_A, verb="Start")),
    (2, _delete_line(B2_TARGET, "backup-0ab05217dd5a4501", VOL_A)),
    (3, _delete_line(R2_TARGET, "backup-962e1433223643a6", VOL_B)),
    (4, _delete_line(B2_TARGET, "backup-2f90667ec84442dc", VOL_A)),
]


def test_parse_backup_deletions_counts_each_b2_deletion_once():
    """`Start` and `Complete` name the same backup ID, so counting both doubles every deletion."""
    found = ledger.parse_backup_deletions(DELETE_LOG, B2_TARGET)
    assert [d["backup"] for d in found] == [
        "backup-0ab05217dd5a4501",
        "backup-2f90667ec84442dc",
    ]
    assert all(d["volume"] == VOL_A for d in found)


def test_parse_backup_deletions_skips_the_r2_target():
    """R2's caps are monthly and vast. Charging an R2 deletion against B2's daily Class C cap
    would inflate the ledger against a cap that does not govern it."""
    found = ledger.parse_backup_deletions(DELETE_LOG, R2_TARGET)
    assert [d["backup"] for d in found] == ["backup-962e1433223643a6"]


def test_parse_backup_deletions_claims_nothing_without_a_target():
    """A blank backupTargetURL is a real state — `k3s_longhorn_backup_armed: false` enforces one.
    Matching everything then would charge both stores to B2."""
    assert ledger.parse_backup_deletions(DELETE_LOG, "") == []


def test_price_deletions_reports_an_unknown_volume_as_unpriced_not_free():
    """A silent zero is indistinguishable from a free deletion, and the transactions were spent."""
    priced, unpriced = ledger.price_deletions(
        [
            {"stamp": 1, "backup": "backup-a", "volume": VOL_A},
            {"stamp": 2, "backup": "backup-b", "volume": VOL_B},
        ],
        {VOL_A: 337},
    )
    assert [(d["backup"], d["class_c"]) for d in priced] == [("backup-a", 337)]
    assert [d["backup"] for d in unpriced] == ["backup-b"]


def test_format_backup_deletions_exits_non_zero_only_when_something_is_unpriced():
    clean, code = ledger.format_backup_deletions(
        [{"backup": "backup-a", "volume": VOL_A, "class_c": 337}],
        [],
        0,
        "26h",
        "2026-09-03T12:19:32Z",
    )
    assert code == 0
    assert "337 Class C" in clean
    flagged, code = ledger.format_backup_deletions(
        [], [{"backup": "backup-b", "volume": VOL_B}], 0, "26h", ""
    )
    assert code == 1
    assert "UNPRICED" in flagged


def test_prune_snapshot_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", str(tmp_path))
    ledger.write_prune_snapshot({VOL_A: {"prune": 337, "blocks": 260}, VOL_B: {}})
    measured_at, prices = ledger.read_prune_snapshot()
    assert prices == {VOL_A: 337}
    assert measured_at.endswith("Z")


def test_read_prune_snapshot_returns_empty_when_none_was_ever_written(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", str(tmp_path))
    assert ledger.read_prune_snapshot() == ("", {})


def test_recorded_deletion_ids_dedupes_across_two_ledger_days(tmp_path, monkeypatch):
    """A deletion just before 00:00 UTC is recorded in one day's file and re-seen by the next
    run, whose file is a different one — so a single-day read would charge it twice."""
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", str(tmp_path))
    (tmp_path / "2026-09-02.tsv").write_text(
        "2026-09-02T23:58:00Z\tb2-deletions\t0\t0\t337\tbackup-old vol=%s priced from x\n"
        % VOL_A
    )
    (tmp_path / "2026-09-03.tsv").write_text(
        "2026-09-03T00:10:00Z\tb2-deletions\t0\t0\t12\tbackup-new vol=%s priced from x\n"
        % VOL_A
        + "2026-09-03T00:11:00Z\tb2-budget\t0\t0\t2\t1 pages\n"
    )
    assert ledger.recorded_deletion_ids(["2026-09-03", "2026-09-02"]) == {
        "backup-old",
        "backup-new",
    }


def test_recorded_deletion_ids_ignores_other_tools_lines(tmp_path, monkeypatch):
    """b2-budget's note carries no backup ID, but a future tool's might — the tool column is
    what decides, not the note's shape."""
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", str(tmp_path))
    (tmp_path / "2026-09-03.tsv").write_text(
        "2026-09-03T00:10:00Z\tsome-drain\t0\t0\t9\tremoved backup-notmine\n"
    )
    assert ledger.recorded_deletion_ids(["2026-09-03"]) == set()


def test_spend_is_charged_to_the_utc_day_it_happened_on(tmp_path, monkeypatch):
    """B2's caps reset per UTC day. A deletion at 23:50 UTC discovered by the next morning's run
    consumed yesterday's cap, so charging it to the run's own day misattributes it."""
    monkeypatch.setattr(ledger, "B2_LEDGER_DIR", str(tmp_path))
    late = int(
        datetime(2026, 9, 2, 23, 50, tzinfo=timezone.utc).timestamp() * 1_000_000_000
    )
    assert ledger.deletion_utc_day(late) == "2026-09-02"
    ledger.record_b2_spend(
        ledger.LEDGER_DELETIONS_TOOL,
        class_c=51,
        note="backup-late",
        day=ledger.deletion_utc_day(late),
    )
    assert (
        ledger.read_b2_ledger("2026-09-02")[ledger.LEDGER_DELETIONS_TOOL]["class_c"]
        == 51
    )
    assert ledger.read_b2_ledger("2026-09-03") == {}


def test_ledger_days_spanning_covers_every_file_the_window_reached():
    """The dedupe read has to cover each day the window could have written into, or a deletion
    charged to an earlier file is charged again on the next run."""
    now = datetime(2026, 9, 3, 12, 10, tzinfo=timezone.utc)
    assert ledger.ledger_days_spanning(3600, _now=now) == ["2026-09-03", "2026-09-02"]
    assert ledger.ledger_days_spanning(26 * 3600, _now=now) == [
        "2026-09-03",
        "2026-09-02",
        "2026-09-01",
    ]
