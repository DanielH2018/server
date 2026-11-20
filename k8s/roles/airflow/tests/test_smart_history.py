"""The SMART rollup's reading choice and its check, against Scrutiny's recorded history.

`fixtures/scrutiny-2026-11-20.json` holds Scrutiny's `/api/summary` and each device's `year`
readings as recorded at 14:51 UTC on 2026-11-20, reduced to the fields the rollup reads. Both
devices hold a reading for every day from 11-15, then for days a few apart, then monthly.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from dags.homelab_warehouse import smart_history as sh

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/scrutiny-2026-11-20.json").read_text()
)
DEVICES = sh.active_devices(FIXTURE["summary"])
CRUCIAL = "2533eadadff7"
NOW = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)


def rows_on(day: date) -> dict[tuple[str, str], float]:
    rows = sh.smart_rows(day, DEVICES, FIXTURE["details"])
    return {(row[1], row[3]): row[4] for row in rows}


def as_dicts(day: date, devices=DEVICES) -> list[dict]:
    return [
        dict(zip(sh.SMART_COLUMNS, row, strict=True))
        for row in sh.smart_rows(day, devices, FIXTURE["details"])
    ]


def test_a_held_day_gives_every_attribute_of_every_device():
    rows = rows_on(date(2026, 11, 19))

    assert len(rows) == 2 * 17
    assert rows[(CRUCIAL, "temp")] == 52
    assert rows[(CRUCIAL, "percentage_used")] == 3


def test_a_day_with_two_readings_keeps_the_later_one():
    # 2026-11-20 holds the 00:00 reading and one taken at 14:51.
    assert rows_on(date(2026, 11, 20))[(CRUCIAL, "data_units_written")] == 68_429_948


def test_a_day_scrutiny_no_longer_holds_gives_no_row():
    assert rows_on(date(2026, 11, 14)) == {}


def test_an_archived_device_is_not_read():
    summary = json.loads(json.dumps(FIXTURE["summary"]))
    summary["summary"][CRUCIAL]["device"]["archived"] = True

    assert set(sh.active_devices(summary)) == {"snb5n481410405d3r"}


def test_the_recorded_days_pass_the_check():
    for day in (date(2026, 11, 19), date(2026, 11, 14), date(2026, 9, 1)):
        assert sh.quality_problems(day, as_dicts(day), DEVICES, NOW) == []


def test_a_recent_day_missing_a_device_fails_the_check():
    day = date(2026, 11, 19)
    rows = [row for row in as_dicts(day) if row["wwn"] != CRUCIAL]

    assert sh.quality_problems(day, rows, DEVICES, NOW) == [
        f"{day}: no reading for {CRUCIAL} (CT1000E100SSD8)"
    ]


def test_an_older_day_missing_a_device_passes_the_check():
    assert sh.quality_problems(date(2026, 11, 10), [], DEVICES, NOW) == []


def test_a_temperature_out_of_bounds_fails_the_check():
    day = date(2026, 11, 19)
    row = {"wwn": CRUCIAL, "attribute": "temp", "value": 120.0}

    assert sh.quality_problems(day, [row], {}, NOW) == [
        f"{day}: {CRUCIAL} read 120.0 C"
    ]
