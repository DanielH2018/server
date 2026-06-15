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
from http.server import BaseHTTPRequestHandler, HTTPServer


def _env(name, default):
    return os.environ.get(name, default)


LOKI_URL = _env("LOKI_URL", "http://loki:3100").rstrip("/")
LOKI_QUERY = _env("LOKI_QUERY", '{container="terraria"}')
POLL_INTERVAL = int(_env("POLL_INTERVAL", "20"))
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "10"))
METRICS_PORT = int(_env("METRICS_PORT", "9420"))
DB_PATH = _env("DB_PATH", "/data/stats.db")
BACKFILL_DAYS = float(_env("BACKFILL_DAYS", "30"))
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
