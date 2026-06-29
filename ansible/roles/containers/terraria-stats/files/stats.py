#!/usr/bin/env python3
"""terraria-stats — all-time player playtime/presence from the Terraria console.

Reads Terraria console lines already ingested into Loki, parses connection
events (join / leave / server-restart), folds them into all-time per-player
playtime + session counts kept in SQLite (the source of truth), and serves
Prometheus metrics for Grafana. Stdlib only (python:3.14-alpine, no deps). The
Terraria container is never touched. Deaths/chat are NOT emitted by the vanilla
console (verified Phase 0, 2026-06-15) and are out of scope.

Design: docs/superpowers/specs/2026-06-15-terraria-player-stats-design.md
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _env(name, default):
    return os.environ.get(name, default)


LOKI_URL = _env("LOKI_URL", "http://loki:3100").rstrip("/")
LOKI_QUERY = _env("LOKI_QUERY", '{container="terraria"}')
POLL_INTERVAL = int(_env("POLL_INTERVAL", "20"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
METRICS_PORT = int(_env("METRICS_PORT", "9420"))
DB_PATH = _env("DB_PATH", "/data/stats.db")
# 28d (672h) stays well under Loki's max_query_length (~721h/30d1h) — the first-run/backfill
# query spans this whole window, so keep headroom below that limit (else HTTP 400).
BACKFILL_DAYS = float(_env("BACKFILL_DAYS", "28"))
LOKI_PAGE_LIMIT = int(_env("LOKI_PAGE_LIMIT", "5000"))
HEALTH_MAX_AGE = int(_env("HEALTH_MAX_AGE", str(3 * POLL_INTERVAL + 30)))

# --- parsing (pure) ---------------------------------------------------------
JOIN_RE = re.compile(r"^(?P<name>.+) has joined\.$")
LEAVE_RE = re.compile(r"^(?P<name>.+) has left\.$")
RESTART_MARKERS = ("Listening on port", "Server started")


def parse_line(line):
    """Classify a console line -> ('join'|'leave', name) | ('restart', None) | None."""
    line = line.rstrip("\r\n")
    m = JOIN_RE.match(line)
    if m:
        return ("join", m.group("name"))
    m = LEAVE_RE.match(line)
    if m:
        return ("leave", m.group("name"))
    for marker in RESTART_MARKERS:
        if line.startswith(marker):
            return ("restart", None)
    return None


def is_unparsed_player_line(line):
    """Drift safety net: looks like a join/leave but did NOT strictly parse.

    Incremented as terraria_stats_unmatched_player_lines_total so a future
    console-wording change surfaces in Grafana instead of silently dropping
    events. Valid lines parse (return None here); noise lacks the keywords.
    """
    if parse_line(line) is not None:
        return False
    low = line.lower()
    return "joined" in low or "has left" in low


# --- state (pure, testable) -------------------------------------------------
class StatsState:
    """In-memory all-time stats. Timestamps are unix seconds (float)."""

    def __init__(self):
        self.players = {}        # name -> dict
        self.last_event_ts = 0.0
        self.unmatched = 0

    def _player(self, name):
        return self.players.setdefault(name, {
            "total_playtime": 0.0,
            "sessions": 0,
            "first_seen": None,
            "last_seen": None,
            "open_start": None,
        })

    def _close(self, name, ts):
        p = self.players.get(name)
        if p and p["open_start"] is not None:
            p["total_playtime"] += max(0.0, ts - p["open_start"])
            p["sessions"] += 1
            p["open_start"] = None
            p["last_seen"] = ts

    def apply(self, kind, name, ts):
        self.last_event_ts = max(self.last_event_ts, ts)
        if kind == "join":
            self._close(name, ts)          # defensive: a rejoin with no leave line
            p = self._player(name)
            p["open_start"] = ts
            if p["first_seen"] is None:
                p["first_seen"] = ts
            p["last_seen"] = ts
        elif kind == "leave":
            self._close(name, ts)
        elif kind == "restart":
            for n in list(self.players):
                self._close(n, ts)

    def online_count(self):
        return sum(1 for p in self.players.values() if p["open_start"] is not None)

    def playtime(self, name, now):
        """Total playtime incl. the in-progress session so the counter ticks live."""
        p = self.players[name]
        base = p["total_playtime"]
        if p["open_start"] is not None:
            base += max(0.0, now - p["open_start"])
        return base


# --- Prometheus exposition (pure) -------------------------------------------
def escape_label_value(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(state, now):
    out = []
    out.append("# HELP terraria_player_playtime_seconds_total Total seconds a player has been connected.")
    out.append("# TYPE terraria_player_playtime_seconds_total counter")
    for name in sorted(state.players):
        out.append('terraria_player_playtime_seconds_total{player="%s"} %d'
                   % (escape_label_value(name), int(state.playtime(name, now))))
    out.append("# HELP terraria_player_sessions_total Completed play sessions.")
    out.append("# TYPE terraria_player_sessions_total counter")
    for name in sorted(state.players):
        out.append('terraria_player_sessions_total{player="%s"} %d'
                   % (escape_label_value(name), state.players[name]["sessions"]))
    out.append("# HELP terraria_players_online Currently connected players.")
    out.append("# TYPE terraria_players_online gauge")
    out.append("terraria_players_online %d" % state.online_count())
    out.append("# HELP terraria_stats_last_event_timestamp Unix time of the last processed event.")
    out.append("# TYPE terraria_stats_last_event_timestamp gauge")
    out.append("terraria_stats_last_event_timestamp %d" % int(state.last_event_ts))
    out.append("# HELP terraria_stats_unmatched_player_lines_total Player-shaped lines that did not parse.")
    out.append("# TYPE terraria_stats_unmatched_player_lines_total counter")
    out.append("terraria_stats_unmatched_player_lines_total %d" % state.unmatched)
    return "\n".join(out) + "\n"


# --- Loki ingestion ---------------------------------------------------------
def extract_entries(loki_json):
    """Flatten a Loki query_range response to [(ts_ns:int, line:str)] ascending."""
    out = []
    for stream in loki_json.get("data", {}).get("result", []):
        for ts, line in stream.get("values", []):
            out.append((int(ts), line))
    out.sort(key=lambda tl: tl[0])
    return out


def apply_entries(state, entries):
    """Apply ascending (ts_ns, line) entries to `state`.

    Returns (events, max_ts_ns) where events is [(ts_ns, name, kind, raw)] for the
    SQLite audit log. Pure: no I/O, so it is unit-tested directly.
    """
    events = []
    max_ts = 0
    for ts_ns, line in entries:
        ev = parse_line(line)
        if ev is not None:
            kind, name = ev
            state.apply(kind, name, ts_ns / 1e9)
            events.append((ts_ns, name, kind, line))
        elif is_unparsed_player_line(line):
            state.unmatched += 1
        if ts_ns > max_ts:
            max_ts = ts_ns
    return events, max_ts


# --- SQLite source of truth -------------------------------------------------
class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        c = self.conn
        c.execute("""CREATE TABLE IF NOT EXISTS players(
            name TEXT PRIMARY KEY,
            total_playtime_seconds REAL NOT NULL DEFAULT 0,
            session_count INTEGER NOT NULL DEFAULT 0,
            first_seen REAL, last_seen REAL,
            current_session_start REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cursor(
            id INTEGER PRIMARY KEY CHECK(id=1), last_ts_ns INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            ts_ns INTEGER, player TEXT, kind TEXT, raw TEXT)""")
        c.commit()

    def load_state(self):
        st = StatsState()
        for name, tot, sess, fs, ls, css in self.conn.execute(
            "SELECT name,total_playtime_seconds,session_count,first_seen,"
            "last_seen,current_session_start FROM players"
        ):
            st.players[name] = {
                "total_playtime": float(tot), "sessions": int(sess),
                "first_seen": fs, "last_seen": ls, "open_start": css}
            # NOTE: last_event_ts is approximated from player last_seen on reload. It can
            # lag the true last-event time if the last event was a server restart with no
            # one online. Only the observability gauge is affected; the cursor drives all
            # correctness decisions.
            if ls:
                st.last_event_ts = max(st.last_event_ts, ls)
        return st

    def get_cursor(self):
        row = self.conn.execute("SELECT last_ts_ns FROM cursor WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def save(self, state, cursor_ns, events=()):
        """Persist events + player snapshot + cursor atomically (single transaction)."""
        c = self.conn
        if events:
            c.executemany(
                "INSERT INTO events(ts_ns,player,kind,raw) VALUES(?,?,?,?)", events)
        for name, p in state.players.items():
            c.execute(
                "INSERT INTO players(name,total_playtime_seconds,session_count,"
                "first_seen,last_seen,current_session_start) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "total_playtime_seconds=excluded.total_playtime_seconds,"
                "session_count=excluded.session_count,first_seen=excluded.first_seen,"
                "last_seen=excluded.last_seen,"
                "current_session_start=excluded.current_session_start",
                (name, p["total_playtime"], p["sessions"], p["first_seen"],
                 p["last_seen"], p["open_start"]))
        c.execute(
            "INSERT INTO cursor(id,last_ts_ns) VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET last_ts_ns=excluded.last_ts_ns", (cursor_ns,))
        c.commit()


# --- HTTP I/O + main loop ---------------------------------------------------
def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "terraria-stats"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 internal
        return json.load(resp)


def loki_fetch(start_ns, end_ns):
    """Fetch entries in (start_ns, end_ns] as [(ts_ns, line)] (one page)."""
    qs = urllib.parse.urlencode({
        "query": LOKI_QUERY, "start": start_ns + 1, "end": end_ns,
        "limit": LOKI_PAGE_LIMIT, "direction": "forward"})
    return extract_entries(http_get_json(LOKI_URL + "/loki/api/v1/query_range?" + qs))


def run_cycle(state, store, cursor, end_ns, fetch):
    """One poll: page through new entries from `cursor`, fold, persist. Returns new cursor.

    `fetch(start_ns, end_ns) -> [(ts_ns, line)]`. Pages until a short/empty page.
    State mutation + cursor advance are persisted together so a crash re-runs the
    batch cleanly (events past the saved cursor simply re-apply on next start).
    """
    while True:
        entries = fetch(cursor, end_ns)
        if not entries:
            break
        events, max_ts = apply_entries(state, entries)
        if max_ts > cursor:
            cursor = max_ts
        store.save(state, cursor, events)
        if len(entries) < LOKI_PAGE_LIMIT:
            break
    return cursor


def log(*args):
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


_state = StatsState()
_lock = threading.Lock()
# _last_poll_ok is written by the poll loop and read by /healthz without a lock. Safe under
# CPython's GIL (float assignment is atomic). If ever run on a free-threaded interpreter,
# guard it with _lock in both places.
_last_poll_ok = 0.0


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/metrics":
                with _lock:
                    body = render_metrics(_state, time.time()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/healthz":
                fresh = (time.time() - _last_poll_ok) < HEALTH_MAX_AGE
                self.send_response(200 if fresh else 503)
                self.end_headers()
                self.wfile.write(b"ok\n" if fresh else b"stale\n")
            else:
                self.send_response(404)
                self.end_headers()
    return Handler


def initial_cursor(stored_cursor, backfill, now, backfill_days):
    """Pick the starting cursor (ns).

    On a fresh DB (stored_cursor==0) or an explicit --backfill, bound the start to the
    last `backfill_days` rather than epoch. A first query spanning 1970->now exceeds
    Loki's max_query_length (~30d) and returns HTTP 400; bounding it also makes the
    first deploy backfill recent history. A normal run resumes from the stored cursor.
    """
    if backfill or stored_cursor == 0:
        return int((now - backfill_days * 86400) * 1e9)
    return stored_cursor


def main():
    global _state, _last_poll_ok
    once = "--once" in sys.argv
    backfill = "--backfill" in sys.argv
    store = Store(DB_PATH)
    with _lock:
        _state = store.load_state()
    cursor = initial_cursor(store.get_cursor(), backfill, time.time(), BACKFILL_DAYS)
    log("terraria-stats starting (loki=%s once=%s backfill=%s players=%d)"
        % (LOKI_URL, once, backfill, len(_state.players)))
    if not (once or backfill):
        # Threading server so a slow /metrics render can't head-of-line-block the /healthz
        # probe (and trip autoheal). Handler reads in-memory _state under _lock, no SQLite.
        threading.Thread(target=lambda: ThreadingHTTPServer(
            ("0.0.0.0", METRICS_PORT), _make_handler()).serve_forever(),
            daemon=True).start()
    while True:
        try:
            end_ns = int(time.time() * 1e9)
            with _lock:
                cursor = run_cycle(_state, store, cursor, end_ns, loki_fetch)
            _last_poll_ok = time.time()
            log("poll ok: %d players, %d online" % (len(_state.players), _state.online_count()))
        except Exception as e:  # an unreachable Loki must not kill the loop
            log("poll error:", e)
        if once or backfill:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
