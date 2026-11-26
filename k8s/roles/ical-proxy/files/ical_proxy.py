"""Flask proxy that republishes ICS calendar feeds Homepage can render directly.

Fetches two Google Calendar ICS feeds and one Obsidian Tasks gist feed on a background
refresh loop, processes each (drops superseded recurrence occurrences, caps overlapping
recurring series, and — for the Obsidian feed — hoists a clickable link, a stable UID and
drops LOCATION per VTODO), and serves the cached, processed result at /calendar{1,2,4}.ics.

Usage:

    python app.py

Takes no arguments. The image copies this file in as app.py and runs it as the container
command. It fetches every feed once, then serves on port 5000 until killed. `-h` and `--help`
print this text and exit 0.

Environment (all optional; the deployment sets them from env-secret and the manifest):

    GOOGLE_ICS_URL      first Google Calendar ICS feed, served at /calendar1.ics
    GOOGLE_ICS_URL_2    second Google Calendar ICS feed, served at /calendar2.ics
    OBSIDIAN_GIST_URL   Obsidian Tasks gist feed, served at /calendar4.ics
    REFRESH_INTERVAL    seconds between feed refreshes (default 900)
"""

import sys

# Answer --help before the imports below, which need Flask, requests and icalendar installed.
if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)

from flask import Flask, Response
import icalendar
import requests, threading, time, os, re, hashlib

from datetime import UTC, datetime, timedelta
from itertools import pairwise

app = Flask(__name__)

GOOGLE_ICS_URL = os.environ.get("GOOGLE_ICS_URL", "")
GOOGLE_ICS_URL_2 = os.environ.get("GOOGLE_ICS_URL_2", "")
OBSIDIAN_GIST_URL = os.environ.get("OBSIDIAN_GIST_URL", "")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "900"))

initial_fetch_done = threading.Event()

cached_ics = {"1": "", "2": "", "4": ""}

# Stop at whitespace or the closing quote of the malformed ALTREP obsidian-ical-plugin puts in
# LOCATION.
OBSIDIAN_URI_RE = re.compile(r'obsidian://[^\s"]+')

# Tags drive Obsidian's Tasks queries and are calendar noise; the leading \s* avoids a double space.
TOD_TAG_RE = re.compile(r"\s*#tod/\S+")


def wire_value(component, name):
    """Returns a property's value as the feed wrote it: escaped, unfolded, stripped; "" if absent."""
    if name not in component:
        return ""
    return component[name].to_ical().decode().strip()


def drop_superseded_masters(calendar):
    """Removes each recurring master whose DTSTART an override of the same UID re-schedules.

    Compares the wire DTSTART and RECURRENCE-ID values, without their TZID, so a feed mixing
    all-day, floating and zoned times never compares a date with a datetime.
    """
    overridden = {
        (str(e["UID"]), wire_value(e, "RECURRENCE-ID"))
        for e in calendar.walk("VEVENT")
        if "UID" in e and "RECURRENCE-ID" in e
    }
    calendar.subcomponents = [
        c
        for c in calendar.subcomponents
        if not (
            c.name == "VEVENT"
            and "RECURRENCE-ID" not in c
            and (str(c.get("UID")), wire_value(c, "DTSTART")) in overridden
        )
    ]


def cap_overlapping_series(calendar):
    """Caps the earlier of two recurring series that share the same SUMMARY and RRULE.

    Sets UNTIL to the day before the later series starts, so the two don't overlap.
    """
    groups = {}
    for event in calendar.walk("VEVENT"):
        rrule, summary = wire_value(event, "RRULE"), wire_value(event, "SUMMARY")
        if rrule and summary:
            groups.setdefault((summary, rrule), []).append(event)

    for group in groups.values():
        group.sort(key=lambda e: wire_value(e, "DTSTART"))
        for earlier, later in pairwise(group):
            start = datetime.strptime(
                wire_value(later, "DTSTART")[:8], "%Y%m%d"
            ).replace(tzinfo=UTC)
            until = start - timedelta(seconds=1)
            earlier["RRULE"]["UNTIL"] = [until]
            print(
                f"Capped recurring event UNTIL={until:%Y%m%dT%H%M%SZ}: SUMMARY={earlier['SUMMARY']}"
            )


def process_ics(raw_ics):
    """Drops superseded recurrence masters and caps overlapping series in one ICS feed."""
    calendar = icalendar.Calendar.from_ical(raw_ics)
    drop_superseded_masters(calendar)
    cap_overlapping_series(calendar)
    return calendar.to_ical().decode()


def obsidian_deep_link(uri):
    """Return a browser-safe obsidian:// link.

    The obsidian-ical-plugin leaves a literal '&' in folder names (e.g. the
    "10 Tasks & Habits" folder). In an <a href> a browser reads that '&' as a
    query-param separator and drops the rest of the path, opening the wrong note.
    Percent-encode any '&' inside the file value so the path reaches Obsidian intact.
    """
    marker = "&file="
    idx = uri.find(marker)
    if idx == -1:
        return uri
    head, path = uri[: idx + len(marker)], uri[idx + len(marker) :]
    return head + path.replace("&", "%26")


def stable_uid(todo):
    """Deterministic UID for a VTODO, derived from its note link + summary.

    The obsidian-to-ical-plugin (v2.0.1, what this feed uses) emits a fresh random
    UID every time it regenerates. Homepage's calendar keys each event by (UID, date)
    and MERGES refetched events into existing state without ever clearing it, so a
    task whose UID changed reappears under a new key — i.e. a duplicate — on a soft
    reload (a full page load remounts and clears the state, which is why dupes only
    show up between refreshes). Rewriting the UID to a hash of the note + summary
    (what newer plugin versions do) keeps the key stable so refetches overwrite.

    Hashes the escaped wire values: hashing the unescaped text would mint a new UID for
    every task whose summary or note holds a comma, semicolon or backslash.
    """
    summary = wire_value(todo, "SUMMARY")
    description = wire_value(todo, "DESCRIPTION")
    return hashlib.sha1(f"{description}::{summary}".encode()).hexdigest()


def process_obsidian_ics(raw_ics):
    """Give each Obsidian VTODO a clickable URL, a stable UID, and drop LOCATION.

    The obsidian-to-ical-plugin emits the note's deep link only in DESCRIPTION and as
    a malformed LOCATION (raw ALTREP text) — never as a URL property — and stamps a
    random UID each regeneration. So Homepage renders the task as plain, non-clickable
    text (showing the ALTREP string on hover) and accumulates duplicates as the UID
    churns. Hoist the obsidian:// link into a real URL property (Homepage then wraps
    the row in an <a href>), replace the UID with a stable hash (see stable_uid), and
    remove LOCATION so hover shows the task title instead.

    Time-block tags come off the SUMMARY first, before the UID is hashed: stable_uid
    derives from the summary, so retagging a task would otherwise mint a new UID and
    leave the old row behind as a duplicate.
    """
    calendar = icalendar.Calendar.from_ical(raw_ics)
    for todo in calendar.walk("VTODO"):
        source = str(todo.get("DESCRIPTION") or todo.get("LOCATION") or "")
        link = OBSIDIAN_URI_RE.search(source)
        if "SUMMARY" in todo:
            todo["SUMMARY"] = icalendar.vText(TOD_TAG_RE.sub("", str(todo["SUMMARY"])))
        if "UID" in todo:
            todo["UID"] = stable_uid(todo)
        todo.pop("LOCATION", None)
        if link and "URL" not in todo:
            todo.add("URL", obsidian_deep_link(link.group(0)))
    return calendar.to_ical().decode()


# The cap stops a hostile or broken upstream ballooning the parse past the container's memory limit.
MAX_FEED_BYTES = 10 * 1024 * 1024  # 10 MB


def refresh_feed(key, url, processor=None):
    """Fetches one ICS feed, processes it, and stores the result in cached_ics[key].

    Streams the response under a byte ceiling (MAX_FEED_BYTES) rather than reading an
    unbounded body into memory. Any fetch or parse error is caught and logged; on failure
    cached_ics[key] is left holding its previous value rather than cleared.

    Args:
        key: The cached_ics dict key this feed's processed output is stored under.
        url: The upstream ICS feed URL to fetch.
        processor: Callable applied to the raw ICS text before caching; defaults to
            process_ics.
    """
    if processor is None:
        processor = process_ics
    try:
        # stream=True + a byte ceiling so we never read an unbounded body into memory.
        with requests.get(
            url, timeout=(10, 30), stream=True
        ) as r:  # 10s connect, 30s read
            if r.status_code == 200:
                chunks, total = [], 0
                for chunk in r.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > MAX_FEED_BYTES:
                        raise ValueError(
                            f"feed exceeded {MAX_FEED_BYTES // (1024 * 1024)}MB cap"
                        )
                    chunks.append(chunk)
                text = b"".join(chunks).decode(r.encoding or "utf-8", "replace")
                cached_ics[key] = processor(text)
                print(f"Calendar {key} refreshed successfully")
            else:
                print(
                    f"Calendar {key}: failed to fetch, HTTP {r.status_code}",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"Calendar {key}: error fetching — {e}", file=sys.stderr)


def refresh_loop():
    """Refreshes every configured feed in a loop, sleeping REFRESH_INTERVAL between passes.

    Sets initial_fetch_done after the first full pass over all feeds (or immediately when no
    ICS URLs are configured), so callers can wait for it before serving a first request. A
    single feed's error is caught and logged; it never stops the loop or the other feeds.
    """
    feeds = [
        (k, u, p)
        for k, u, p in [
            ("1", GOOGLE_ICS_URL, process_ics),
            ("2", GOOGLE_ICS_URL_2, process_ics),
            ("4", OBSIDIAN_GIST_URL, process_obsidian_ics),
        ]
        if u
    ]
    if not feeds:
        print("No ICS URLs configured.")
        initial_fetch_done.set()
        return
    while True:
        for key, url, processor in feeds:
            try:
                refresh_feed(key, url, processor)
            except Exception as e:
                print(f"Unexpected error for calendar {key}: {e}")
            time.sleep(1)  # brief pause between feeds to avoid overwhelming source
        if not initial_fetch_done.is_set():
            initial_fetch_done.set()
        print("All feeds done, sleeping until next refresh...")
        time.sleep(REFRESH_INTERVAL)


@app.route("/calendar1.ics")
def serve_ics_1():
    return Response(cached_ics["1"], mimetype="text/calendar")


@app.route("/calendar2.ics")
def serve_ics_2():
    return Response(cached_ics["2"], mimetype="text/calendar")


@app.route("/calendar4.ics")
def serve_ics_4():
    return Response(cached_ics["4"], mimetype="text/calendar")


if __name__ == "__main__":
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()
    initial_fetch_done.wait()  # blocks until all feeds have been fetched once
    print("Initial fetch complete, starting Flask...")
    app.run(host="0.0.0.0", port=5000)
