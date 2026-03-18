from flask import Flask, Response
import requests, threading, time, os, re

from datetime import datetime, timedelta

app = Flask(__name__)

GOOGLE_ICS_URL = os.environ.get("GOOGLE_ICS_URL", "")
GOOGLE_ICS_URL_2 = os.environ.get("GOOGLE_ICS_URL_2", "")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", 900))

initial_fetch_done = threading.Event()

cached_ics = {"1": "", "2": ""}

def parse_vevent_blocks(ics_text):
    """Split ICS into header, list of VEVENT strings, and footer."""
    blocks = re.split(r'(?=BEGIN:VEVENT)', ics_text)
    header = blocks[0]
    footer = ""
    events = []
    for block in blocks[1:]:
        match = re.search(r'(BEGIN:VEVENT.*?END:VEVENT)(.*)', block, re.DOTALL)
        if match:
            events.append(match.group(1))
            trailing = match.group(2).strip()
            if trailing:
                footer += trailing
    if not footer:
        footer = "END:VCALENDAR"
    return header, events, footer

def get_prop(vevent, prop):
    """Extract a property value from a VEVENT block, handling folded lines."""
    unfolded = re.sub(r'\r?\n[ \t]', '', vevent)
    match = re.search(rf'^{prop}(?:;[^:\n]*)?:(.+)$', unfolded, re.MULTILINE)
    return match.group(1).strip() if match else None

def normalize_dt(dt_value):
    """Strip timezone prefix if present, e.g. 'America/New_York:20210120T113000' -> '20210120T113000'"""
    if dt_value and ':' in dt_value:
        return dt_value.split(':')[-1]
    return dt_value

def filter_superseded_occurrences(events):
    """Remove occurrences of recurring events that have been overridden."""
    overrides = {}
    for vevent in events:
        uid = get_prop(vevent, 'UID')
        recurrence_id = get_prop(vevent, 'RECURRENCE-ID')
        if uid and recurrence_id:
            normalized = normalize_dt(recurrence_id)
            overrides.setdefault(uid, set()).add(normalized)

    filtered = []
    for vevent in events:
        uid = get_prop(vevent, 'UID')
        recurrence_id = get_prop(vevent, 'RECURRENCE-ID')
        dtstart = get_prop(vevent, 'DTSTART')

        if recurrence_id:
            filtered.append(vevent)
            continue

        if uid and dtstart and uid in overrides:
            normalized_dtstart = normalize_dt(dtstart)
            if normalized_dtstart in overrides[uid]:
                continue

        filtered.append(vevent)
    return filtered

def dedup_overlapping_recurrences(events):
    """
    For recurring events sharing the same SUMMARY and RRULE, cap the earlier
    series with UNTIL set to the day before the later series starts.
    """


    # Group recurring events by (SUMMARY, RRULE)
    groups = {}
    for vevent in events:
        rrule = get_prop(vevent, 'RRULE')
        summary = get_prop(vevent, 'SUMMARY')
        if not rrule or not summary:
            continue
        key = (summary, rrule)
        groups.setdefault(key, []).append(vevent)

    # For any group with more than one series, cap all but the latest
    to_cap = {}  # vevent index -> UNTIL value
    for key, group in groups.items():
        if len(group) < 2:
            continue
        # Sort by DTSTART
        def get_dtstart(v):
            raw = normalize_dt(get_prop(v, 'DTSTART'))
            return raw or ''
        group.sort(key=get_dtstart)
        # Cap each earlier series at the day before the next one starts
        for i in range(len(group) - 1):
            later_dtstart = normalize_dt(get_prop(group[i + 1], 'DTSTART'))
            dt = datetime.strptime(later_dtstart[:8], '%Y%m%d')
            until = (dt - timedelta(days=1)).strftime('%Y%m%dT235959Z')
            to_cap[id(group[i])] = until

    patched = []
    for vevent in events:
        if id(vevent) in to_cap:
            until = to_cap[id(vevent)]
            vevent = re.sub(r'(RRULE:[^\r\n]+)', rf'\1;UNTIL={until}', vevent)
            print(f"Capped recurring event UNTIL={until}: SUMMARY={get_prop(vevent, 'SUMMARY')}")
        patched.append(vevent)
    return patched

def process_ics(raw_ics):
    """Parse, filter, and reassemble the ICS."""
    header, events, footer = parse_vevent_blocks(raw_ics)
    filtered = filter_superseded_occurrences(events)
    filtered = dedup_overlapping_recurrences(filtered)
    return header + '\r\n'.join(filtered) + '\r\n' + footer

def refresh_feed(key, url):
    try:
        r = requests.get(url, timeout=(10, 30))  # 10s to connect, 30s to read
        if r.status_code == 200:
            cached_ics[key] = process_ics(r.text)
            print(f"Calendar {key} refreshed successfully")
        else:
            print(f"Calendar {key}: failed to fetch, HTTP {r.status_code}")
    except Exception as e:
        print(f"Calendar {key}: error fetching — {e}")

def refresh_loop():
    feeds = [(k, u) for k, u in [("1", GOOGLE_ICS_URL), ("2", GOOGLE_ICS_URL_2)] if u]
    if not feeds:
        print("No ICS URLs configured.")
        initial_fetch_done.set()
        return
    while True:
        for key, url in feeds:
            try:
                refresh_feed(key, url)
            except Exception as e:
                print(f"Unexpected error for calendar {key}: {e}")
            time.sleep(1) # brief pause between feeds to avoid overwhelming source
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

if __name__ == "__main__":
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()
    initial_fetch_done.wait()  # blocks until all feeds have been fetched once
    print("Initial fetch complete, starting Flask...")
    app.run(host="0.0.0.0", port=5000)
