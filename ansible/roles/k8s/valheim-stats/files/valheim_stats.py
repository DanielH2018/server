#!/usr/bin/env python3
"""valheim-stats — all-time player playtime/sessions/DEATHS from the Valheim console.

Sibling of terraria-stats and deliberately the same shape: read console lines already
ingested into Loki, fold them into all-time per-player stats kept in SQLite (the source
of truth), serve Prometheus metrics for Grafana. Stdlib only (python:3.14-alpine, no
deps). The Valheim container is never touched.

Why it is a fork rather than a shared library: Valheim's console is a different language
from Terraria's, not a dialect of it. Terraria names the player on both join and leave;
Valheim names them only on spawn, identifies the disconnect by SteamID, and emits deaths
(which Terraria's console cannot — see the terraria-stats docstring). That pushes a
SteamID<->name mapping and a death counter into the state model, so the state machine
differs more than the parsing does.

Line formats (corroborated across the image's own valheim-logfilter, adaliszk's mtail
program, and mbround18/valheim-docker; NOT yet observed on this server, which had no
players at the time of writing — that is what the unmatched-line metric is for):

    Got handshake from client 76561198108936133      <- connect
    Got character ZDOID from Testvazz : 954855457:113 <- spawn (also fires on RESPAWN)
    Got character ZDOID from Testvazz : 0:0           <- death
    Closing socket 76561198108936133                  <- disconnect

Lines arrive carrying the image's own supervisord prefix, e.g.
    Aug 13 16:53:42 supervisord: valheim-server Got character ZDOID from X : 0:0
so every pattern is SEARCHED, never anchored at ^. (Terraria's parser anchors because
its image logs bare lines. Anchoring here would match nothing.)
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


LOKI_URL = _env("LOKI_URL", "http://loki-homelab:3100").rstrip("/")
LOKI_QUERY = _env("LOKI_QUERY", '{container="valheim"}')
POLL_INTERVAL = int(_env("POLL_INTERVAL", "20"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
METRICS_PORT = int(_env("METRICS_PORT", "9420"))
DB_PATH = _env("DB_PATH", "/data/stats.db")
# Same ceiling as terraria-stats: stay under Loki's max_query_length (~721h) or the
# first-run query 400s. Valheim has no history to backfill (it was archived long before
# this Loki existed), so this only ever covers the current server's uptime.
BACKFILL_DAYS = float(_env("BACKFILL_DAYS", "28"))
LOKI_PAGE_LIMIT = int(_env("LOKI_PAGE_LIMIT", "5000"))
HEALTH_MAX_AGE = int(_env("HEALTH_MAX_AGE", str(3 * POLL_INTERVAL + 30)))

# parsing (pure)
HANDSHAKE_RE = re.compile(r"Got handshake from client (?P<steam_id>\d{5,25})")
# Non-greedy name up to the ` : ` separator. The ZDO id is signed in the wild, and the
# `0:0` form is the death sentinel — captured here and split by kind below rather than
# by a second regex, so a name containing " : " can never make the two disagree.
ZDOID_RE = re.compile(
    r"Got character ZDOID from (?P<name>.+?) : (?P<zdo>-?\d+):(?P<idx>\d+)\s*$"
)
CLOSING_RE = re.compile(r"Closing socket (?P<steam_id>\d{5,25})")
# World load is the one unambiguous "the server is fresh" marker: it appears once per
# start, after which no pre-restart session can still be open.
RESTART_RE = re.compile(r"Load world: ")
# Periodic server heartbeat, every ~10 min. Independent, directly-observed truth about
# how many peers are connected — see valheim_connections in render_metrics.
CONNECTIONS_RE = re.compile(r"Connections (?P<n>\d+) ZDOS:(?P<zdos>\d+)")


def parse_line(line):
    """Classify a console line.

    -> ('connect', steam_id) | ('spawn', name) | ('death', name)
       | ('disconnect', steam_id) | ('restart', None) | ('heartbeat', count) | None
    """
    line = line.rstrip("\r\n")
    m = ZDOID_RE.search(line)
    if m:
        name = m.group("name")
        # `0:0` means the character's ZDO was destroyed — the death sentinel. Any other
        # id is a spawn, which ALSO fires on respawn, so the state machine must treat a
        # spawn while a session is already open as a no-op rather than a new session.
        if m.group("zdo") == "0" and m.group("idx") == "0":
            return ("death", name)
        return ("spawn", name)
    m = HANDSHAKE_RE.search(line)
    if m:
        return ("connect", m.group("steam_id"))
    m = CLOSING_RE.search(line)
    if m:
        return ("disconnect", m.group("steam_id"))
    m = CONNECTIONS_RE.search(line)
    if m:
        return ("heartbeat", int(m.group("n")))
    if RESTART_RE.search(line):
        return ("restart", None)
    return None


def is_unparsed_player_line(line):
    """Drift safety net: looks player-shaped but did NOT parse.

    Exposed as valheim_stats_unmatched_player_lines_total so an upstream wording change
    surfaces in Grafana instead of silently dropping events. This matters more here than
    it does for terraria-stats: these patterns were taken from documentation and other
    projects, not observed on this server, so the first real play session is what
    confirms them. A non-zero counter with no player metrics is the signature of a
    format mismatch.
    """
    if parse_line(line) is not None:
        return False
    low = line.lower()
    return "zdoid" in low or "handshake from client" in low or "closing socket" in low


# state (pure, testable)
class StatsState:
    """In-memory all-time stats. Timestamps are unix seconds (float)."""

    def __init__(self):
        self.players = {}  # name -> dict
        self.last_event_ts = 0.0
        self.unmatched = 0
        self.connections = 0  # last heartbeat value (directly observed)
        # SteamID <-> name, needed because a disconnect names only the SteamID.
        self.steam_to_name = {}
        self.pending_steam_id = None

    def _player(self, name):
        return self.players.setdefault(
            name,
            {
                "total_playtime": 0.0,
                "sessions": 0,
                "deaths": 0,
                "first_seen": None,
                "last_seen": None,
                "open_start": None,
            },
        )

    def _close(self, name, ts):
        p = self.players.get(name)
        if p and p["open_start"] is not None:
            p["total_playtime"] += max(0.0, ts - p["open_start"])
            p["sessions"] += 1
            p["open_start"] = None
            p["last_seen"] = ts

    def apply(self, kind, value, ts):
        """Applies one console event (connect/spawn/death/disconnect/restart/heartbeat).

        connect holds a SteamID until the next spawn names the character (the console
        never puts both on one line). spawn opens a new session, unless one is already
        open (a respawn after death, not a second session). disconnect closes the
        session bound to the SteamID's last-known name. restart closes every open
        session and clears the SteamID<->name mapping. heartbeat records the server's
        own connection count.

        Args:
            kind: One of "connect", "spawn", "death", "disconnect", "restart",
                "heartbeat".
            value: The SteamID (connect/disconnect), player name (spawn/death), or
                connection count (heartbeat); ignored for "restart".
            ts: The event's Unix timestamp, in seconds.
        """
        self.last_event_ts = max(self.last_event_ts, ts)
        if kind == "connect":
            # Held until the next spawn names the character. The binding is ADJACENCY —
            # the handshake and the spawn that follows it — which is how the other
            # published parsers do it too, because the console never puts the SteamID
            # and the character name on the same line. Two players completing a
            # handshake before either spawns can therefore cross-bind; with a homelab
            # player count that is rare and self-corrects on their next join. Playtime
            # is the only thing affected: deaths are attributed by name directly.
            self.pending_steam_id = value
        elif kind == "spawn":
            p = self._player(value)
            if self.pending_steam_id is not None:
                self.steam_to_name[self.pending_steam_id] = value
                self.pending_steam_id = None
            if p["open_start"] is None:
                # New session. A spawn with a session already open is a RESPAWN after
                # death and must not count as a second session.
                p["open_start"] = ts
                if p["first_seen"] is None:
                    p["first_seen"] = ts
            p["last_seen"] = ts
        elif kind == "death":
            p = self._player(value)
            p["deaths"] += 1
            p["last_seen"] = ts
        elif kind == "disconnect":
            name = self.steam_to_name.get(value)
            if name is not None:
                self._close(name, ts)
        elif kind == "restart":
            for n in list(self.players):
                self._close(n, ts)
            self.steam_to_name.clear()
            self.pending_steam_id = None
        elif kind == "heartbeat":
            self.connections = value

    def online_count(self):
        return sum(1 for p in self.players.values() if p["open_start"] is not None)

    def total_deaths(self):
        return sum(p["deaths"] for p in self.players.values())

    def playtime(self, name, now):
        """Total playtime incl. the in-progress session so the counter ticks live."""
        p = self.players[name]
        base = p["total_playtime"]
        if p["open_start"] is not None:
            base += max(0.0, now - p["open_start"])
        return base


# Prometheus exposition (pure)
def escape_label_value(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(state, now):
    """Renders `state` as Prometheus text exposition format.

    Args:
        state: The StatsState to render.
        now: The current Unix timestamp, used to include in-progress session time in
            each player's playtime.

    Returns:
        The full exposition text, HELP/TYPE lines included, newline-terminated.
    """
    out = []
    out.append(
        "# HELP valheim_player_playtime_seconds_total Total seconds a player has been connected."
    )
    out.append("# TYPE valheim_player_playtime_seconds_total counter")
    for name in sorted(state.players):
        out.append(
            'valheim_player_playtime_seconds_total{player="%s"} %d'
            % (escape_label_value(name), int(state.playtime(name, now)))
        )
    out.append("# HELP valheim_player_sessions_total Completed play sessions.")
    out.append("# TYPE valheim_player_sessions_total counter")
    for name in sorted(state.players):
        out.append(
            'valheim_player_sessions_total{player="%s"} %d'
            % (escape_label_value(name), state.players[name]["sessions"])
        )
    out.append("# HELP valheim_player_deaths_total Times a player has died.")
    out.append("# TYPE valheim_player_deaths_total counter")
    for name in sorted(state.players):
        out.append(
            'valheim_player_deaths_total{player="%s"} %d'
            % (escape_label_value(name), state.players[name]["deaths"])
        )
    out.append("# HELP valheim_deaths_total Deaths across all players.")
    out.append("# TYPE valheim_deaths_total counter")
    out.append("valheim_deaths_total %d" % state.total_deaths())
    out.append(
        "# HELP valheim_players_online Currently connected players, derived from session tracking."
    )
    out.append("# TYPE valheim_players_online gauge")
    out.append("valheim_players_online %d" % state.online_count())
    # Cross-check gauge, not a duplicate. valheim_players_online is DERIVED from the
    # session state machine; this is the number the server itself reports in its ~10 min
    # heartbeat. They should agree, and a persistent disagreement is the cheapest signal
    # that the session logic (whose join/leave lines could not be validated before first
    # play) has drifted — worth more than either number alone.
    out.append(
        "# HELP valheim_connections Peers the server itself reported at its last heartbeat."
    )
    out.append("# TYPE valheim_connections gauge")
    out.append("valheim_connections %d" % state.connections)
    out.append(
        "# HELP valheim_stats_last_event_timestamp Unix time of the last processed event."
    )
    out.append("# TYPE valheim_stats_last_event_timestamp gauge")
    out.append("valheim_stats_last_event_timestamp %d" % int(state.last_event_ts))
    out.append(
        "# HELP valheim_stats_unmatched_player_lines_total Player-shaped lines that did not parse."
    )
    out.append("# TYPE valheim_stats_unmatched_player_lines_total counter")
    out.append("valheim_stats_unmatched_player_lines_total %d" % state.unmatched)
    return "\n".join(out) + "\n"


# Loki ingestion
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

    Returns (events, max_ts_ns) where events is [(ts_ns, subject, kind, raw)] for the
    SQLite audit log. Pure: no I/O, so it is unit-tested directly.
    """
    events = []
    max_ts = 0
    for ts_ns, line in entries:
        ev = parse_line(line)
        if ev is not None:
            kind, value = ev
            state.apply(kind, value, ts_ns / 1e9)
            # Heartbeats are every-10-min noise with no per-player meaning; keeping them
            # out of the audit table stops it dwarfing the events worth reading.
            if kind != "heartbeat":
                events.append((ts_ns, str(value), kind, line))
        elif is_unparsed_player_line(line):
            state.unmatched += 1
        if ts_ns > max_ts:
            max_ts = ts_ns
    return events, max_ts


# SQLite source of truth
class Store:
    """SQLite-backed source of truth for player stats, the ingest cursor, and the raw event log."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_schema(self):
        c = self.conn
        c.execute("""CREATE TABLE IF NOT EXISTS players(
            name TEXT PRIMARY KEY,
            total_playtime_seconds REAL NOT NULL DEFAULT 0,
            session_count INTEGER NOT NULL DEFAULT 0,
            death_count INTEGER NOT NULL DEFAULT 0,
            first_seen REAL, last_seen REAL,
            current_session_start REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cursor(
            id INTEGER PRIMARY KEY CHECK(id=1), last_ts_ns INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            ts_ns INTEGER, player TEXT, kind TEXT, raw TEXT)""")
        # Survives a restart so a session that spans one is still attributable, and so a
        # disconnect arriving after a stats restart can still resolve to a name.
        c.execute("""CREATE TABLE IF NOT EXISTS steam_names(
            steam_id TEXT PRIMARY KEY, name TEXT NOT NULL)""")
        c.commit()

    def load_state(self):
        """Loads all players and the SteamID<->name mapping from SQLite into a fresh StatsState.

        Returns:
            A StatsState populated from the players and steam_names tables, with
            last_event_ts approximated from each player's last_seen (see the note below
            for its one blind spot).
        """
        st = StatsState()
        for name, tot, sess, deaths, fs, ls, css in self.conn.execute(
            "SELECT name,total_playtime_seconds,session_count,death_count,first_seen,"
            "last_seen,current_session_start FROM players"
        ):
            st.players[name] = {
                "total_playtime": float(tot),
                "sessions": int(sess),
                "deaths": int(deaths),
                "first_seen": fs,
                "last_seen": ls,
                "open_start": css,
            }
            # Same caveat as terraria-stats: last_event_ts is approximated from last_seen
            # on reload and can lag. Only the observability gauge is affected; the cursor
            # drives every correctness decision.
            if ls:
                st.last_event_ts = max(st.last_event_ts, ls)
        for steam_id, name in self.conn.execute(
            "SELECT steam_id,name FROM steam_names"
        ):
            st.steam_to_name[steam_id] = name
        return st

    def get_cursor(self):
        row = self.conn.execute("SELECT last_ts_ns FROM cursor WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def save(self, state, cursor_ns, events=()):
        """Persist events + player snapshot + cursor atomically (single transaction)."""
        c = self.conn
        if events:
            c.executemany(
                "INSERT INTO events(ts_ns,player,kind,raw) VALUES(?,?,?,?)", events
            )
        for name, p in state.players.items():
            c.execute(
                "INSERT INTO players(name,total_playtime_seconds,session_count,"
                "death_count,first_seen,last_seen,current_session_start) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "total_playtime_seconds=excluded.total_playtime_seconds,"
                "session_count=excluded.session_count,"
                "death_count=excluded.death_count,first_seen=excluded.first_seen,"
                "last_seen=excluded.last_seen,"
                "current_session_start=excluded.current_session_start",
                (
                    name,
                    p["total_playtime"],
                    p["sessions"],
                    p["deaths"],
                    p["first_seen"],
                    p["last_seen"],
                    p["open_start"],
                ),
            )
        for steam_id, name in state.steam_to_name.items():
            c.execute(
                "INSERT INTO steam_names(steam_id,name) VALUES(?,?) "
                "ON CONFLICT(steam_id) DO UPDATE SET name=excluded.name",
                (steam_id, name),
            )
        c.execute(
            "INSERT INTO cursor(id,last_ts_ns) VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET last_ts_ns=excluded.last_ts_ns",
            (cursor_ns,),
        )
        c.commit()


# HTTP I/O + main loop
def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-stats"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def loki_fetch(start_ns, end_ns):
    """Fetch entries in (start_ns, end_ns] as [(ts_ns, line)] (one page)."""
    qs = urllib.parse.urlencode(
        {
            "query": LOKI_QUERY,
            "start": start_ns + 1,
            "end": end_ns,
            "limit": LOKI_PAGE_LIMIT,
            "direction": "forward",
        }
    )
    return extract_entries(http_get_json(LOKI_URL + "/loki/api/v1/query_range?" + qs))


def run_cycle(state, store, cursor, end_ns, fetch):
    """One poll: page through new entries from `cursor`, fold, persist. Returns new cursor.

    `fetch(start_ns, end_ns) -> [(ts_ns, line)]`. Pages until a short/empty page.
    State mutation + cursor advance are persisted together so a crash re-runs the batch
    cleanly (events past the saved cursor simply re-apply on next start).
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
# Written by the poll loop, read by /healthz without a lock. Safe under CPython's GIL
# (float assignment is atomic). On a free-threaded interpreter, guard with _lock in both.
_last_poll_ok = 0.0


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        """Serves /metrics (Prometheus exposition) and /healthz (poll staleness)."""

        # `format` is the parameter name BaseHTTPRequestHandler.log_message declares.
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self):
            """Routes the request path to /metrics, /healthz, or a 404."""
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
    last `backfill_days` rather than epoch: a first query spanning 1970->now exceeds
    Loki's max_query_length and returns HTTP 400. A normal run resumes from the cursor.
    """
    if backfill or stored_cursor == 0:
        return int((now - backfill_days * 86400) * 1e9)
    return stored_cursor


def main():
    """Loads persisted state, starts the metrics server, and runs the poll loop.

    Runs a single cycle and returns when invoked with --once or --backfill; otherwise
    starts a background HTTP server for /metrics and /healthz and polls Loki forever at
    POLL_INTERVAL. A poll cycle's own exception is caught and logged rather than
    allowed to kill the loop.
    """
    global _state, _last_poll_ok
    once = "--once" in sys.argv
    backfill = "--backfill" in sys.argv
    store = Store(DB_PATH)
    with _lock:
        _state = store.load_state()
    cursor = initial_cursor(store.get_cursor(), backfill, time.time(), BACKFILL_DAYS)
    log(
        "valheim-stats starting (loki=%s once=%s backfill=%s players=%d)"
        % (LOKI_URL, once, backfill, len(_state.players))
    )
    if not (once or backfill):
        # Threading server so a slow /metrics render cannot head-of-line-block /healthz.
        threading.Thread(
            target=lambda: ThreadingHTTPServer(
                ("0.0.0.0", METRICS_PORT), _make_handler()
            ).serve_forever(),
            daemon=True,
        ).start()
    while True:
        try:
            end_ns = int(time.time() * 1e9)
            with _lock:
                cursor = run_cycle(_state, store, cursor, end_ns, loki_fetch)
            _last_poll_ok = time.time()
            log(
                "poll ok: %d players, %d online, %d deaths"
                % (len(_state.players), _state.online_count(), _state.total_deaths())
            )
        except Exception as e:  # an unreachable Loki must not kill the loop
            log("poll error:", e)
        if once or backfill:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
