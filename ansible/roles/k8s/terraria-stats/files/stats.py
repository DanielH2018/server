#!/usr/bin/env python3
"""terraria-stats — all-time player playtime/presence from the Terraria console.

Reads Terraria console lines already ingested into Loki, parses connection
events (join / leave / server-restart), folds them into all-time per-player
playtime + session counts kept in SQLite (the source of truth), and serves
Prometheus metrics for Grafana. Stdlib only (python:3.14-alpine, no deps). The
Terraria container is never touched. Deaths/chat are NOT emitted by the vanilla
console (verified Phase 0, 2026-06-15) and are out of scope.

The Loki fetch, cursor handling, metric rendering/escaping, the HTTP handler, the run loop
and the env reader are shared with valheim-stats via `stats_lib` (see that module's
docstring and `roles/k8s/game-stats-lib/tasks/stage.yml` for how it gets here). What stays
here — the line parser, the state machine and the SQLite schema — is genuinely per-game:
Terraria names the player on both join and leave and has no death/SteamID bookkeeping,
where Valheim names a player only on spawn, resolves a disconnect by SteamID, and tracks
deaths (see valheim_stats.py's docstring).

Design: docs/superpowers/specs/2026-06-15-terraria-player-stats-design.md
"""

import re
import sqlite3
import sys
import time

import stats_lib

_env = stats_lib.env
log = stats_lib.log
extract_entries = stats_lib.extract_entries
initial_cursor = stats_lib.initial_cursor
escape_label_value = stats_lib.escape_label_value

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

# parsing (pure) — the genuinely per-game part; see the module docstring.
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


# state (pure, testable) — per-game: no death/SteamID bookkeeping (Terraria emits neither).
class StatsState:
    """In-memory all-time stats. Timestamps are unix seconds (float)."""

    def __init__(self):
        self.players = {}  # name -> dict
        self.last_event_ts = 0.0
        self.unmatched = 0

    def _player(self, name):
        return self.players.setdefault(
            name,
            {
                "total_playtime": 0.0,
                "sessions": 0,
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

    def apply(self, kind, name, ts):
        """Applies one connection event (join/leave/restart) to the in-memory state.

        A join with no matching leave (a dangling open_start) is closed defensively
        before the new session opens. A restart closes every currently open session.

        Args:
            kind: One of "join", "leave", "restart".
            name: The player name the event applies to (ignored for "restart").
            ts: The event's Unix timestamp, in seconds.
        """
        self.last_event_ts = max(self.last_event_ts, ts)
        if kind == "join":
            self._close(name, ts)  # defensive: a rejoin with no leave line
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
    stats_lib.render_family(
        out,
        "terraria_player_playtime_seconds_total",
        "Total seconds a player has been connected.",
        "counter",
        [(name, int(state.playtime(name, now))) for name in sorted(state.players)],
        label_name="player",
    )
    stats_lib.render_family(
        out,
        "terraria_player_sessions_total",
        "Completed play sessions.",
        "counter",
        [(name, state.players[name]["sessions"]) for name in sorted(state.players)],
        label_name="player",
    )
    stats_lib.render_family(
        out,
        "terraria_players_online",
        "Currently connected players.",
        "gauge",
        [state.online_count()],
    )
    stats_lib.render_family(
        out,
        "terraria_stats_last_event_timestamp",
        "Unix time of the last processed event.",
        "gauge",
        [int(state.last_event_ts)],
    )
    stats_lib.render_family(
        out,
        "terraria_stats_unmatched_player_lines_total",
        "Player-shaped lines that did not parse.",
        "counter",
        [state.unmatched],
    )
    return "\n".join(out) + "\n"


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


# SQLite source of truth — per-game: no death/SteamID columns (Terraria has neither).
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
            first_seen REAL, last_seen REAL,
            current_session_start REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cursor(
            id INTEGER PRIMARY KEY CHECK(id=1), last_ts_ns INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            ts_ns INTEGER, player TEXT, kind TEXT, raw TEXT)""")
        c.commit()

    def load_state(self):
        """Loads all players from SQLite into a fresh StatsState.

        Returns:
            A StatsState populated from the players table, with last_event_ts
            approximated from each player's last_seen (see the note below for its one
            blind spot).
        """
        st = StatsState()
        for name, tot, sess, fs, ls, css in self.conn.execute(
            "SELECT name,total_playtime_seconds,session_count,first_seen,"
            "last_seen,current_session_start FROM players"
        ):
            st.players[name] = {
                "total_playtime": float(tot),
                "sessions": int(sess),
                "first_seen": fs,
                "last_seen": ls,
                "open_start": css,
            }
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
                "INSERT INTO events(ts_ns,player,kind,raw) VALUES(?,?,?,?)", events
            )
        for name, p in state.players.items():
            c.execute(
                "INSERT INTO players(name,total_playtime_seconds,session_count,"
                "first_seen,last_seen,current_session_start) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "total_playtime_seconds=excluded.total_playtime_seconds,"
                "session_count=excluded.session_count,first_seen=excluded.first_seen,"
                "last_seen=excluded.last_seen,"
                "current_session_start=excluded.current_session_start",
                (
                    name,
                    p["total_playtime"],
                    p["sessions"],
                    p["first_seen"],
                    p["last_seen"],
                    p["open_start"],
                ),
            )
        c.execute(
            "INSERT INTO cursor(id,last_ts_ns) VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET last_ts_ns=excluded.last_ts_ns",
            (cursor_ns,),
        )
        c.commit()


# Loki fetch + the run loop — thin per-game bindings over stats_lib's shared skeleton.
def loki_fetch(start_ns, end_ns):
    """Fetch entries in (start_ns, end_ns] as [(ts_ns, line)] (one page)."""
    url = stats_lib.build_query_range_url(
        LOKI_URL, LOKI_QUERY, start_ns, end_ns, LOKI_PAGE_LIMIT
    )
    return extract_entries(stats_lib.http_get_json(url, HTTP_TIMEOUT, "terraria-stats"))


def run_cycle(state, store, cursor, end_ns, fetch):
    """One poll: page through new entries from `cursor`, fold, persist. Returns new cursor.

    Thin wrapper over stats_lib.run_cycle binding this game's apply_entries + page limit —
    see that function's docstring for the paging/persistence contract.
    """
    return stats_lib.run_cycle(
        state, store, cursor, end_ns, fetch, apply_entries, LOKI_PAGE_LIMIT
    )


def main():
    """Loads persisted state, starts the metrics server, and runs the poll loop.

    Runs a single cycle and returns when invoked with --once or --backfill; otherwise
    starts a background HTTP server for /metrics and /healthz and polls Loki forever at
    POLL_INTERVAL. A poll cycle's own exception is caught and logged rather than
    allowed to kill the loop.
    """
    once = "--once" in sys.argv
    backfill = "--backfill" in sys.argv
    store = Store(DB_PATH)
    poll_state = stats_lib.PollState(store.load_state())
    cursor = initial_cursor(store.get_cursor(), backfill, time.time(), BACKFILL_DAYS)
    log(
        "terraria-stats starting (loki=%s once=%s backfill=%s players=%d)"
        % (LOKI_URL, once, backfill, len(poll_state.value.players))
    )
    if not (once or backfill):
        # Threading server so a slow /metrics render can't head-of-line-block the /healthz
        # probe (and trip autoheal). Handler reads in-memory state under the lock, no SQLite.
        stats_lib.start_metrics_server(
            stats_lib.make_handler(poll_state, render_metrics, HEALTH_MAX_AGE),
            METRICS_PORT,
        )
    stats_lib.poll_forever(
        poll_state,
        store,
        cursor,
        loki_fetch,
        apply_entries,
        LOKI_PAGE_LIMIT,
        once,
        backfill,
        POLL_INTERVAL,
        lambda state: (
            "%d players, %d online" % (len(state.players), state.online_count())
        ),
    )


if __name__ == "__main__":
    main()
