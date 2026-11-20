"""Roll each disk's SMART readings for a day into `smart.attribute_daily`, and check them.

Scrutiny keeps a reading for every day only for about the last week. Before that it keeps
points days apart, and then one a month, so the warehouse's nightly copy is the only daily
record. A day's row per device and attribute is the latest reading Scrutiny holds dated that
day. A day it no longer holds a reading for gets no row, which is how the catchup loads
Scrutiny's sparser older points once.

Rows are long-form, one per attribute, because NVMe and ATA disks report different
attribute sets. A device is named by its WWN and model; its device name repeats across hosts.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    devices = active_devices(fetch_summary())
    details = {wwn: fetch_details(wwn) for wwn in devices}
    rows = smart_rows(day, devices, details)
"""

import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from .prom_rollup import read_json

SCRUTINY_URL = "http://scrutiny.homelab.svc.cluster.local:8080"
# The days before today Scrutiny keeps a reading for each one of.
DAILY_DAYS = 6
TEMP_BOUNDS_C = (0.0, 90.0)

SCHEMA = "smart"
SMART_TABLE = f"{SCHEMA}.attribute_daily"
SMART_COLUMNS = ("day", "wwn", "model", "attribute", "value")
DDL = (
    f"CREATE TABLE IF NOT EXISTS {SMART_TABLE} (day date NOT NULL, wwn text NOT NULL, "
    "model text NOT NULL, attribute text NOT NULL, value double precision NOT NULL, "
    "PRIMARY KEY (day, wwn, attribute))",
)


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def is_complete(day: date, now: datetime) -> bool:
    """Whether `day` has ended, so its last reading is final."""
    return _start(day) + timedelta(days=1) <= now


def in_daily_window(day: date, now: datetime) -> bool:
    """Whether Scrutiny still holds a reading for every day as recent as `day`."""
    return now - timedelta(days=DAILY_DAYS) <= _start(day)


def _data(body: Mapping) -> Mapping:
    if not body.get("success"):
        raise RuntimeError(f"Scrutiny refused the request: {body}")
    return body["data"]


def fetch_summary(
    base_url: str = SCRUTINY_URL, opener: Callable = urllib.request.urlopen
) -> Mapping:
    """Scrutiny's `/api/summary` data: each device's record, keyed by WWN."""
    return _data(read_json(f"{base_url}/api/summary", opener))


def fetch_details(
    wwn: str, base_url: str = SCRUTINY_URL, opener: Callable = urllib.request.urlopen
) -> Mapping:
    """One device's readings over Scrutiny's `year` range, newest first."""
    url = f"{base_url}/api/device/{wwn}/details?duration_key=year"
    return _data(read_json(url, opener))


def active_devices(summary: Mapping) -> dict[str, str]:
    """Each device Scrutiny has not archived, as its model keyed by WWN."""
    return {
        wwn: entry["device"].get("model_name", "")
        for wwn, entry in summary["summary"].items()
        if not entry["device"].get("archived")
    }


def _reading_on(day: date, results: Sequence[Mapping]) -> Mapping | None:
    start = _start(day)
    held = [
        (at, r)
        for r in results
        if start
        <= (at := datetime.fromisoformat(r["date"]))
        < start + timedelta(days=1)
    ]
    return max(held, key=lambda pair: pair[0], default=(None, None))[1]


def smart_rows(
    day: date, devices: Mapping[str, str], details: Mapping[str, Mapping]
) -> list[tuple]:
    """One `attribute_daily` row per device and attribute with a reading dated `day`."""
    rows = []
    for wwn, model in sorted(devices.items()):
        reading = _reading_on(day, details[wwn]["smart_results"])
        if reading is None:
            continue
        values = {
            "temp": reading["temp"],
            "power_on_hours": reading["power_on_hours"],
            **{name: attr["value"] for name, attr in reading["attrs"].items()},
        }
        rows += [
            (day, wwn, model, name, float(value))
            for name, value in sorted(values.items())
        ]
    return rows


def quality_problems(
    day: date, rows: Sequence[Mapping], devices: Mapping[str, str], now: datetime
) -> list[str]:
    """Every reason the day's `attribute_daily` rows are not trustworthy; empty when they are.

    Inside the daily window every active device must have readings, so a missing one means
    its collector did not report. Before it, Scrutiny holds only some days, so none is
    required.
    """
    problems = []
    seen = {row["wwn"] for row in rows}
    if in_daily_window(day, now):
        problems += [
            f"{day}: no reading for {wwn} ({model})"
            for wwn, model in sorted(devices.items())
            if wwn not in seen
        ]
    low, high = TEMP_BOUNDS_C
    for row in rows:
        if row["attribute"] == "temp" and not low <= row["value"] <= high:
            problems.append(f"{day}: {row['wwn']} read {row['value']} C")
    return problems
