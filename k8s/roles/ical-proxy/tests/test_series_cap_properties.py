"""Properties of `cap_overlapping_series` over all-day, floating, UTC and zoned series.

dateutil's rrule expansion is the oracle. A capped series is expanded from the feed
`process_ics` serves, so every property also proves the output still parses.

Run: uv run pytest k8s/roles/ical-proxy/tests/test_series_cap_properties.py
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from itertools import takewhile
from zoneinfo import ZoneInfo

import icalendar
from dateutil.rrule import rrulestr
from hypothesis import example, given
from hypothesis import strategies as st

import ical_proxy as app

ZONES = (
    "America/Los_Angeles",
    "America/Chicago",
    "America/New_York",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Kolkata",
    "Asia/Tokyo",
    "Pacific/Auckland",
)
HORIZON = timedelta(days=21)

series = st.tuples(
    st.sampled_from(("date", "floating", "utc", "tzid")),
    st.sampled_from(ZONES),
    st.integers(0, 23),
)
pairs = given(
    earlier=series,
    later=series,
    start=st.dates(date(2024, 1, 1), date(2027, 12, 31)),
    gap=st.integers(1, 60),
    freq=st.sampled_from(("DAILY", "WEEKLY")),
)
WEST_EVENING = {
    "earlier": ("tzid", "America/Chicago", 20),
    "later": ("tzid", "America/Chicago", 20),
    "start": date(2026, 3, 1),
    "gap": 14,
    "freq": "DAILY",
}
EAST_MORNING = {**WEST_EVENING, "earlier": ("tzid", "Asia/Tokyo", 2)}
ALL_DAY = {**WEST_EVENING, "earlier": ("date", "UTC", 0), "later": ("date", "UTC", 0)}


def dtstart(spec, day):
    kind, zone, hour = spec
    if kind == "date":
        return day
    moment = datetime.combine(day, time(hour))
    if kind == "floating":
        return moment
    return moment.replace(tzinfo=UTC if kind == "utc" else ZoneInfo(zone))


def feed(earlier, later, start, gap, freq):
    calendar = icalendar.Calendar()
    calendar.add("VERSION", "2.0")
    for uid, spec, day in (
        ("old", earlier, start),
        ("new", later, start + timedelta(days=gap)),
    ):
        event = icalendar.Event()
        event.add("UID", uid)
        event.add("SUMMARY", "Gym")
        event.add("DTSTART", dtstart(spec, day))
        event.add("RRULE", {"FREQ": freq})
        calendar.add_component(event)
    return calendar.to_ical().decode()


def occurrences(event, horizon):
    """Expands an event's RRULE up to the local date horizon, with dateutil."""
    first = event.decoded("DTSTART")
    if not isinstance(first, datetime):
        first = datetime.combine(first, time())
    rule = rrulestr(app.wire_value(event, "RRULE"), dtstart=first)
    return list(takewhile(lambda o: o.date() < horizon, rule))


def capped(earlier, later, start, gap, freq):
    """Returns the original feed's events and the capped feed's, each keyed by UID."""
    raw = feed(earlier, later, start, gap, freq)
    parse = lambda ics: {
        str(e["UID"]): e for e in icalendar.Calendar.from_ical(ics).walk("VEVENT")
    }
    return parse(raw), parse(app.process_ics(raw))


@pairs
@example(**WEST_EVENING)
@example(**EAST_MORNING)
def test_the_capped_series_shares_no_day_with_the_later_one(**draw):
    _, after = capped(**draw)
    cut = draw["start"] + timedelta(days=draw["gap"])

    assert all(o.date() < cut for o in occurrences(after["old"], cut + HORIZON))


@pairs
@example(**WEST_EVENING)
@example(**EAST_MORNING)
def test_the_capped_series_keeps_every_occurrence_before_the_later_start_date(**draw):
    before, after = capped(**draw)
    cut = draw["start"] + timedelta(days=draw["gap"])
    uncapped = occurrences(before["old"], cut + HORIZON)

    assert occurrences(after["old"], cut + HORIZON) == [
        o for o in uncapped if o.date() < cut
    ]


@pairs
@example(**ALL_DAY)
def test_until_takes_the_value_type_of_the_series_dtstart(**draw):
    _, after = capped(**draw)
    rule = dict(
        part.split("=") for part in app.wire_value(after["old"], "RRULE").split(";")
    )
    shape = {"date": r"\d{8}", "floating": r"\d{8}T\d{6}"}.get(
        draw["earlier"][0], r"\d{8}T\d{6}Z"
    )

    assert re.fullmatch(shape, rule["UNTIL"])
