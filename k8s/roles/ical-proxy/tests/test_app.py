"""Unit tests for the ical-proxy feed transforms in ical_proxy.py.

Run: uv run pytest k8s/roles/ical-proxy/tests
(or `uv run pytest` for the whole repo suite).

The obsidian-ical-plugin ships tasks as VTODOs whose only deep link lives in
DESCRIPTION / a malformed LOCATION, never a URL property — so Homepage renders
them as plain, non-clickable text. process_obsidian_ics hoists that link into a
real URL property (which Homepage turns into an <a href>) and cleans up the row.
"""

import hashlib
from datetime import date

import icalendar
import pytest

import ical_proxy as app

CRLF = "\r\n"

OBS = "obsidian://open?vault=My_Vault&file=10%20Tasks%20&%20Habits/Source.md"
OBS_ENCODED = "obsidian://open?vault=My_Vault&file=10%20Tasks%20%26%20Habits/Source.md"


def _todo(summary, description, location=None, uid="test-uid"):
    lines = [
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        "DTSTAMP:20260719T000000",
    ]
    if location is not None:
        lines.append(f"LOCATION:{location}")
    lines += [
        "DUE;VALUE=DATE:20260719",
        "STATUS:NEEDS-ACTION",
        f"DESCRIPTION:{description}",
        "END:VTODO",
    ]
    return CRLF.join(lines)


def _calendar(*todos):
    return CRLF.join(["BEGIN:VCALENDAR", "VERSION:2.0", *todos, "END:VCALENDAR"]) + CRLF


def _todos(feed):
    return icalendar.Calendar.from_ical(feed).walk("VTODO")


def _only_todo(feed):
    (todo,) = _todos(feed)
    return todo


def _extract_uid(feed):
    return str(_only_todo(feed)["UID"])


def test_adds_url_property_from_description():
    out = app.process_obsidian_ics(
        _calendar(_todo("🔲 Shave", OBS, location=f'ALTREP="{OBS}":{OBS}'))
    )
    assert str(_only_todo(out)["URL"]) == OBS_ENCODED


def test_ampersand_inside_file_is_percent_encoded():
    url = str(_only_todo(app.process_obsidian_ics(_calendar(_todo("t", OBS))))["URL"])
    assert "%26" in url
    # the legitimate vault/file query separator stays a bare '&'
    assert "vault=My_Vault&file=" in url


def test_removes_location_line():
    out = app.process_obsidian_ics(
        _calendar(_todo("t", OBS, location=f'ALTREP="{OBS}":{OBS}'))
    )
    assert "LOCATION" not in _only_todo(out)


def test_preserves_summary_including_status_and_priority_emoji():
    out = app.process_obsidian_ics(_calendar(_todo("🔲 Pay Mortgage ⬆️", OBS)))
    assert str(_only_todo(out)["SUMMARY"]) == "🔲 Pay Mortgage ⬆️"


def test_single_end_vcalendar():
    out = app.process_obsidian_ics(_calendar(_todo("a", OBS), _todo("b", OBS)))
    assert out.count("END:VCALENDAR") == 1


def test_does_not_add_second_url_when_one_exists():
    todo = CRLF.join(
        [
            "BEGIN:VTODO",
            "UID:x",
            "SUMMARY:has url",
            f"URL:{OBS_ENCODED}",
            f"DESCRIPTION:{OBS}",
            "END:VTODO",
        ]
    )
    out = app.process_obsidian_ics(_calendar(todo))
    assert str(_only_todo(out)["URL"]) == OBS_ENCODED


def test_todo_without_obsidian_link_is_untouched():
    out = app.process_obsidian_ics(_calendar(_todo("plain", "no link here")))
    assert "URL" not in _only_todo(out)


def test_uid_is_stable_across_regenerations_with_different_source_uids():
    # The plugin stamps a fresh random UID each regeneration; the rewritten UID must
    # depend only on the task's note + summary so Homepage overwrites instead of
    # accumulating duplicates on soft reloads.
    a = app.process_obsidian_ics(_calendar(_todo("🔲 Shave", OBS, uid="random-aaaa")))
    b = app.process_obsidian_ics(_calendar(_todo("🔲 Shave", OBS, uid="random-bbbb")))
    assert _extract_uid(a) == _extract_uid(b)
    assert _extract_uid(a) not in ("random-aaaa", "random-bbbb")


def test_uid_differs_for_different_tasks():
    shave = _extract_uid(app.process_obsidian_ics(_calendar(_todo("🔲 Shave", OBS))))
    mow = _extract_uid(app.process_obsidian_ics(_calendar(_todo("🔲 Mow lawn", OBS))))
    assert shave != mow


def test_strips_time_of_day_tag_from_summary():
    # Real shape: the tag sits between the description and the priority emoji
    out = app.process_obsidian_ics(
        _calendar(_todo("🔲 Clean PC & Screens #tod/afternoon ⬆️", OBS))
    )
    assert str(_only_todo(out)["SUMMARY"]) == "🔲 Clean PC & Screens ⬆️"
    assert "#tod/" not in out


def test_strips_trailing_time_of_day_tag_without_leaving_whitespace():
    out = app.process_obsidian_ics(_calendar(_todo("🔲 Shave #tod/evening", OBS)))
    assert str(_only_todo(out)["SUMMARY"]) == "🔲 Shave"


def test_uid_is_stable_when_time_of_day_tag_changes():
    # Retagging must not mint a new UID: Homepage merges refetched events without clearing state, so
    # a changed UID leaves a duplicate.
    morning = app.process_obsidian_ics(
        _calendar(_todo("🔲 Clean Retainer #tod/morning", OBS))
    )
    evening = app.process_obsidian_ics(
        _calendar(_todo("🔲 Clean Retainer #tod/evening", OBS))
    )
    assert _extract_uid(morning) == _extract_uid(evening)


def test_leaves_unrelated_tags_alone():
    out = app.process_obsidian_ics(_calendar(_todo("🔲 Buy onions #produce", OBS)))
    assert str(_only_todo(out)["SUMMARY"]) == "🔲 Buy onions #produce"


def test_obsidian_deep_link_encodes_only_file_ampersands():
    assert app.obsidian_deep_link(OBS) == OBS_ENCODED
    # no file marker → returned unchanged
    assert (
        app.obsidian_deep_link("obsidian://open?vault=V") == "obsidian://open?vault=V"
    )


@pytest.mark.parametrize(
    ("summary", "description"),
    [
        ("🔲 Pay rent\\, utilities", OBS),
        ("🔲 Pay rent, utilities", OBS),
        ("🔲 Pay rent;utilities", OBS),
        ("🔲 Pay rent #tod/morning", f"{OBS}\\N"),
        ("🔲 Pay rent", f"{OBS} tail, comma"),
    ],
)
def test_uid_hashes_the_summary_and_note_as_the_feed_wrote_them(summary, description):
    # A UID that moves when the feed is re-parsed duplicates every task on Homepage's next soft reload.
    out = app.process_obsidian_ics(_calendar(_todo(summary, description)))
    untagged = summary.removesuffix(" #tod/morning")
    expected = hashlib.sha1(f"{description}::{untagged}".encode()).hexdigest()
    assert _extract_uid(out) == expected


def _event(*lines):
    return CRLF.join(["BEGIN:VEVENT", *lines, "END:VEVENT"])


def _events(feed):
    return icalendar.Calendar.from_ical(feed).walk("VEVENT")


def _rrule(event) -> icalendar.vRecur:
    rule = event["RRULE"]
    assert isinstance(rule, icalendar.vRecur)
    return rule


def test_an_override_of_the_first_occurrence_keeps_the_master():
    feed = _calendar(
        _event(
            "UID:s",
            "DTSTART;TZID=America/New_York:20260201T090000",
            "RRULE:FREQ=DAILY;COUNT=3",
            "SUMMARY:x",
        ),
        _event(
            "UID:s",
            "RECURRENCE-ID;TZID=America/New_York:20260201T090000",
            "DTSTART;TZID=America/New_York:20260201T110000",
            "SUMMARY:x",
        ),
    )
    kept = [
        (str(e["UID"]), "RECURRENCE-ID" in e) for e in _events(app.process_ics(feed))
    ]
    assert kept == [("s", False), ("s", True)]


def test_the_earlier_of_two_matching_series_ends_the_day_before_the_later_starts():
    feed = _calendar(
        _event(
            "UID:new",
            "DTSTART;VALUE=DATE:20260301",
            "RRULE:FREQ=WEEKLY;BYDAY=TU",
            "SUMMARY:Gym",
        ),
        _event(
            "UID:old",
            "DTSTART;VALUE=DATE:20250101",
            "RRULE:FREQ=WEEKLY;BYDAY=TU",
            "SUMMARY:Gym",
        ),
    )
    rules = {str(e["UID"]): _rrule(e) for e in _events(app.process_ics(feed))}
    assert rules["old"]["UNTIL"] == [date(2026, 2, 28)]
    assert "UNTIL" not in rules["new"]


def test_a_folded_rrule_keeps_every_part_when_capped():
    folded = ("RRULE:FREQ=WEEKLY;BYDAY=TU,TH;INTER", " VAL=2;WKST=SU")
    feed = _calendar(
        _event("UID:old", "DTSTART;VALUE=DATE:20250101", *folded, "SUMMARY:Gym"),
        _event("UID:new", "DTSTART;VALUE=DATE:20260301", *folded, "SUMMARY:Gym"),
    )
    rules = {str(e["UID"]): _rrule(e) for e in _events(app.process_ics(feed))}
    assert rules["old"]["INTERVAL"] == [2]
    assert rules["old"]["UNTIL"] == [date(2026, 2, 28)]


def test_series_with_different_rules_are_not_capped():
    feed = _calendar(
        _event(
            "UID:a",
            "DTSTART;VALUE=DATE:20250101",
            "RRULE:FREQ=WEEKLY;BYDAY=TU",
            "SUMMARY:Gym",
        ),
        _event(
            "UID:b",
            "DTSTART;VALUE=DATE:20260301",
            "RRULE:FREQ=WEEKLY;BYDAY=TH",
            "SUMMARY:Gym",
        ),
    )
    assert all("UNTIL" not in _rrule(e) for e in _events(app.process_ics(feed)))
