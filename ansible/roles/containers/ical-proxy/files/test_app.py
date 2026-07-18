"""Unit tests for the ical-proxy Obsidian transform in app.py.

Run: uv run pytest ansible/roles/containers/ical-proxy/files
(or `uv run pytest` for the whole repo suite).

The obsidian-ical-plugin ships tasks as VTODOs whose only deep link lives in
DESCRIPTION / a malformed LOCATION, never a URL property — so Homepage renders
them as plain, non-clickable text. process_obsidian_ics hoists that link into a
real URL property (which Homepage turns into an <a href>) and cleans up the row.
"""

import re

import app

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


def _extract_uid(feed):
    return re.search(r"^UID:(.+)$", feed, re.MULTILINE).group(1).strip()


def test_adds_url_property_from_description():
    out = app.process_obsidian_ics(
        _calendar(_todo("🔲 Shave", OBS, location=f'ALTREP="{OBS}":{OBS}'))
    )
    assert f"URL:{OBS_ENCODED}{CRLF}END:VTODO" in out


def test_ampersand_inside_file_is_percent_encoded():
    out = app.process_obsidian_ics(_calendar(_todo("t", OBS)))
    assert "%26" in out
    # the legitimate vault/file query separator stays a bare '&'
    assert "vault=My_Vault&file=" in out


def test_removes_location_line():
    out = app.process_obsidian_ics(
        _calendar(_todo("t", OBS, location=f'ALTREP="{OBS}":{OBS}'))
    )
    assert "LOCATION:" not in out


def test_preserves_summary_including_status_and_priority_emoji():
    out = app.process_obsidian_ics(_calendar(_todo("🔲 Pay Mortgage ⬆️", OBS)))
    assert "SUMMARY:🔲 Pay Mortgage ⬆️" in out


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
    assert out.count(f"{CRLF}URL:") == 1


def test_todo_without_obsidian_link_is_untouched():
    out = app.process_obsidian_ics(_calendar(_todo("plain", "no link here")))
    assert "URL:" not in out


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


def test_obsidian_deep_link_encodes_only_file_ampersands():
    assert app.obsidian_deep_link(OBS) == OBS_ENCODED
    # no file marker → returned unchanged
    assert (
        app.obsidian_deep_link("obsidian://open?vault=V") == "obsidian://open?vault=V"
    )
