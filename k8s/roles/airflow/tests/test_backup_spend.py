"""The Longhorn backup rollup's line parsing, its Volume join and its retention window.

`fixtures/longhorn-backups-2026-11-19.json` holds the seven backup lines loki-homelab kept for
2026-11-19 UTC, and the Longhorn Volume objects of those seven volumes, reduced to the fields
the join reads.
"""

import io
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from dags.homelab_warehouse import backup_spend as bs

DAY = date(2026, 11, 19)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/longhorn-backups-2026-11-19.json").read_text()
)
LOKI_HOMELAB_DEFAULTS = Path(__file__).parents[2] / "loki-homelab/defaults/main.yml"
HA_VOLUME = "pvc-a840bbef-b0d6-4ca6-8740-b4f64ed05658"


def recorded_rows() -> dict[str, tuple]:
    rows = bs.backup_rows(DAY, FIXTURE["lines"], bs.volumes_by_name(FIXTURE["volumes"]))
    return {row[1]: row[2:] for row in rows}


def loki_returning(lines: list[str]):
    def opener(url, timeout):
        body = {
            "status": "success",
            "data": {"result": [{"values": [["1", line] for line in lines]}]},
        }
        return io.BytesIO(json.dumps(body).encode())

    return opener


def test_each_backup_line_is_a_row_with_its_claim_target_and_class_b():
    rows = recorded_rows()

    assert len(rows) == 7
    assert rows[HA_VOLUME] == ("homelab/home-assistant-config", "r2", 1, 106, 105)
    assert {target for _, target, *_ in rows.values()} == {"r2", "default"}


def test_a_line_without_its_replica_prefix_still_counts():
    line = FIXTURE["lines"][0].split("] ", 1)[1]

    rows = bs.backup_rows(DAY, [line], {})

    assert [row[1:] for row in rows] == [(bs.UNATTRIBUTED, "", "", 1, 106, 105)]


def test_a_volume_with_no_volume_object_gets_no_claim_or_target():
    rows = bs.backup_rows(DAY, FIXTURE["lines"], {})

    assert {row[2:4] for row in rows} == {("", "")}


def test_a_full_page_of_backup_lines_is_refused():
    opener = loki_returning(["x"] * bs.PAGE_LIMIT)

    with pytest.raises(RuntimeError, match="the limit"):
        bs.fetch_backup_lines("http://loki", DAY, opener=opener)


def test_the_volume_read_sends_the_pods_token(tmp_path):
    (tmp_path / "token").write_text("t0ken\n")
    (tmp_path / "ca.crt").write_text("")
    seen = {}

    def opener(request, timeout, context):
        seen["auth"] = request.get_header("Authorization")
        seen["url"] = request.full_url
        return io.BytesIO(json.dumps({"items": FIXTURE["volumes"]}).encode())

    def no_ca(cafile):
        seen["ca"] = cafile

    items = bs.fetch_volumes(
        "https://api", str(tmp_path), opener=opener, ssl_context=no_ca
    )

    assert len(items) == 7
    assert seen == {
        "auth": "Bearer t0ken",
        "url": f"https://api{bs.VOLUMES_PATH}",
        "ca": f"{tmp_path}/ca.crt",
    }


def test_the_backfill_window_stays_a_day_inside_loki_homelabs_retention():
    configured = re.search(
        r"^loki_homelab_retention:\s*(\d+)h", LOKI_HOMELAB_DEFAULTS.read_text(), re.M
    )
    now = datetime(2026, 12, 20, 6, tzinfo=timezone.utc)

    assert configured is not None
    assert int(configured.group(1)) == bs.LOKI_KEEP_DAYS * 24
    assert bs.in_backfill_window(date(2026, 11, 21), now)
    assert not bs.in_backfill_window(date(2026, 11, 20), now)
    assert not bs.in_backfill_window(date(2026, 12, 20), now)


def test_a_day_without_backups_or_with_more_uploaded_than_walked_fails_the_check():
    bad = {"volume": "v", "class_b": 4, "new_blocks": 5}

    assert bs.quality_problems(DAY, []) == [f"{DAY}: no backup line"]
    assert bs.quality_problems(DAY, [bad]) == [
        f"{DAY}: v uploaded 5 blocks of 4 walked"
    ]


def test_the_recorded_day_passes_the_check():
    rows = [
        dict(zip(bs.BACKUP_COLUMNS, row, strict=True))
        for row in bs.backup_rows(
            DAY, FIXTURE["lines"], bs.volumes_by_name(FIXTURE["volumes"])
        )
    ]

    assert bs.quality_problems(DAY, rows) == []
