"""Roll one day of Home Assistant sensor history up into `ha.sensor_daily`, and check it.

HA records a state when it changes, not on a clock, so each state holds from its
`last_changed` until the next one. A day's mean is weighted by how long each value held, and
the time a sensor spent `unavailable` or `unknown` replaces Prometheus' gap as the measure of
missing data. A `total` sensor, such as a plug's cumulative kWh, also yields the day's
increase.

The recorder keeps `HA_KEEP_DAYS` days, so a day older than that is skipped, and a DAG
outage longer than that loses days for good.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    history = fetch_history(HA_URL, token, day, [s.entity_id for s in SENSORS])
    rows = sensor_rows(day, history)
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

HA_URL = "http://home-assistant.homelab.svc.cluster.local:8123"
# The recorder's purge_keep_days (k8s/roles/home-assistant/files/configuration.yaml).
HA_KEEP_DAYS = 7

SCHEMA = "ha"
SENSOR_TABLE = f"{SCHEMA}.sensor_daily"
SENSOR_COLUMNS = (
    "day",
    "entity_id",
    "kind",
    "min",
    "avg",
    "max",
    "increase",
    "changes",
    "unavailable_s",
)
DDL = (
    f"CREATE TABLE IF NOT EXISTS {SENSOR_TABLE} (day date NOT NULL, "
    "entity_id text NOT NULL, kind text NOT NULL, "
    '"min" double precision NOT NULL, avg double precision NOT NULL, '
    '"max" double precision NOT NULL, increase double precision, '
    "changes integer NOT NULL, unavailable_s integer NOT NULL, "
    "PRIMARY KEY (day, entity_id))",
)

# kube-router admits a new pod's traffic a few seconds after the pod starts (ADR 0009).
CONNECT_ATTEMPTS = 6
CONNECT_BACKOFF_S = 5.0


@dataclass(frozen=True)
class Sensor:
    """One HA sensor the warehouse keeps.

    Attributes:
      entity_id: The HA entity.
      bounds: The range every value must fall in, inclusive, in the entity's own unit.
      total: Whether the state is a cumulative counter, whose daily increase is kept.
      max_unavailable_s: The most a day may spend `unavailable` or `unknown`.
    """

    entity_id: str
    bounds: tuple[float, float]
    total: bool = False
    max_unavailable_s: int = 4 * 3600


FAHRENHEIT = (30.0, 110.0)
PERCENT = (0.0, 100.0)
WATTS = (0.0, 3000.0)
KWH = (0.0, 1_000_000.0)

SENSORS = (
    Sensor("sensor.bedroom_airgradient_one_temperature", FAHRENHEIT),
    Sensor("sensor.bedroom_airgradient_one_humidity", PERCENT),
    Sensor("sensor.bedroom_airgradient_one_carbon_dioxide", (300.0, 10000.0)),
    Sensor("sensor.bedroom_airgradient_one_pm2_5", (0.0, 1000.0)),
    Sensor("sensor.bedroom_airgradient_one_voc_index", (0.0, 500.0)),
    Sensor("sensor.aqara_fp300_temperature", FAHRENHEIT),
    Sensor("sensor.aqara_fp300_humidity", PERCENT),
    Sensor("sensor.aqara_fp300_illuminance", (0.0, 100000.0)),
    Sensor("sensor.outdoor_pm2_5", (0.0, 1000.0)),
    Sensor("sensor.air_purifier_power", WATTS),
    Sensor("sensor.airgradient_lamp_power", WATTS),
    Sensor("sensor.behind_bed_power", WATTS),
    Sensor("sensor.desk_surge_protector_strip_power", WATTS),
    Sensor("sensor.air_purifier_energy", KWH, total=True),
    Sensor("sensor.airgradient_lamp_energy", KWH, total=True),
    Sensor("sensor.behind_bed_energy", KWH, total=True),
    Sensor("sensor.desk_surge_protector_strip_energy", KWH, total=True),
)

# A plug draws at most 3 kW, so no day adds more than 72 kWh.
MAX_DAILY_INCREASE_KWH = 72.0


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def in_backfill_window(day: date, now: datetime) -> bool:
    """Whether `day` is complete and the recorder still holds all of it."""
    start = day_start(day)
    return (
        now - timedelta(days=HA_KEEP_DAYS) < start and start + timedelta(days=1) <= now
    )


def history_url(base_url: str, day: date, entity_ids: Sequence[str]) -> str:
    """The `/api/history/period` URL for `entity_ids` over `day`, one list per entity."""
    start = day_start(day)
    params = urllib.parse.urlencode(
        {
            "end_time": (start + timedelta(days=1)).isoformat(),
            "filter_entity_id": ",".join(entity_ids),
            "minimal_response": "",
            "no_attributes": "",
        }
    )
    return f"{base_url}/api/history/period/{start.isoformat()}?{params}"


def fetch_history(
    base_url: str,
    token: str,
    day: date,
    entity_ids: Sequence[str],
    opener: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> list[list[dict]]:
    """HA's state history for `entity_ids` over `day`.

    Retries a refused or unreachable connection `CONNECT_ATTEMPTS` times. An HTTP error is not
    retried: the request, not the network, is wrong.

    Raises:
      urllib.error.URLError: The connection failed on every attempt.
    """
    request = urllib.request.Request(
        history_url(base_url, day, entity_ids),
        headers={"Authorization": f"Bearer {token}"},
    )
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            with opener(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            sleep(CONNECT_BACKOFF_S * (attempt + 1))
    raise AssertionError("unreachable")


def _number(state: str) -> float | None:
    try:
        return float(state)
    except ValueError:
        return None


def sensor_row(sensor: Sensor, day: date, states: Sequence[Mapping]) -> tuple | None:
    """The day's `sensor_daily` row for one entity's history; None when no state was a number.

    Args:
      sensor: The entity's declaration.
      day: The day `states` covers.
      states: HA's history for the entity, oldest first. The first state holds from 00:00.
    """
    start = day_start(day)
    end = start + timedelta(days=1)
    held = []
    for index, state in enumerate(states):
        since = max(datetime.fromisoformat(state["last_changed"]), start)
        until = (
            datetime.fromisoformat(states[index + 1]["last_changed"])
            if index + 1 < len(states)
            else end
        )
        held.append(
            (_number(state["state"]), (min(until, end) - since).total_seconds())
        )
    numbers = [(value, seconds) for value, seconds in held if value is not None]
    if not numbers:
        return None
    weighted_s = sum(seconds for _, seconds in numbers)
    values = [value for value, _ in numbers]
    avg = (
        sum(value * seconds for value, seconds in numbers) / weighted_s
        if weighted_s
        else sum(values) / len(values)
    )
    increase = None
    if sensor.total:
        increase = 0.0
        for previous, current in zip(values, values[1:], strict=False):
            # A counter that drops was reset, and counts up again from zero.
            increase += current - previous if current >= previous else current
        increase = round(increase, 4)
    return (
        day,
        sensor.entity_id,
        "total" if sensor.total else "measurement",
        min(values),
        round(avg, 4),
        max(values),
        increase,
        len(states) - 1,
        round(sum(seconds for value, seconds in held if value is None)),
    )


def sensor_rows(
    day: date, history: Sequence[Sequence[Mapping]], sensors: Sequence[Sensor] = SENSORS
) -> list[tuple]:
    """One row per declared sensor that `history` holds a number for."""
    by_entity = {states[0]["entity_id"]: states for states in history if states}
    rows = []
    for sensor in sensors:
        row = sensor_row(sensor, day, by_entity.get(sensor.entity_id, []))
        if row is not None:
            rows.append(row)
    return rows


def quality_problems(
    day: date, rows: Sequence[Mapping], sensors: Sequence[Sensor] = SENSORS
) -> list[str]:
    """Every reason the day's `sensor_daily` rows are not trustworthy; empty when they are.

    Args:
      day: The day checked.
      rows: The day's rows, read back from the warehouse.
      sensors: The sensors the day must hold.
    """
    by_entity = {row["entity_id"]: row for row in rows}
    problems = []
    for sensor in sensors:
        row = by_entity.get(sensor.entity_id)
        name = sensor.entity_id
        if row is None:
            problems.append(f"{day}: no row for {name}")
            continue
        if row["unavailable_s"] > sensor.max_unavailable_s:
            problems.append(
                f"{day}: {name} was unavailable for {row['unavailable_s'] // 60} minutes"
            )
        low, high = sensor.bounds
        if not low <= row["min"] <= row["max"] <= high:
            problems.append(
                f"{day}: {name} {row['min']}-{row['max']} outside [{low}, {high}]"
            )
        if sensor.total and not 0 <= row["increase"] <= MAX_DAILY_INCREASE_KWH:
            problems.append(
                f"{day}: {name} increased {row['increase']} kWh, outside "
                f"[0, {MAX_DAILY_INCREASE_KWH}]"
            )
    return problems
