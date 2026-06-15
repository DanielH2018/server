# Terraria Player Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a headless `terraria-stats` sidecar that reads Terraria console lines from Loki, tracks all-time per-player playtime/sessions/presence in SQLite, and exposes Prometheus metrics for a Grafana dashboard — without ever modifying the Terraria container.

**Architecture:** A stdlib-only Python service polls Loki's `query_range` API for `{container="terraria"}` lines, parses join/leave/server-restart events (pure functions), folds them into an in-memory `StatsState` persisted to SQLite (the durable source of truth), and serves `/metrics` (hand-emitted Prometheus text) + `/healthz` over HTTP. Prometheus scrapes it; a provisioned Grafana dashboard renders it. Deployed as a normal container role on the `monitoring` network, mirroring the `monitor-bridge` precedent.

**Tech Stack:** Python 3.14 (stdlib only: `urllib`, `json`, `sqlite3`, `http.server`, `re`, `threading`), Ansible, Docker Compose, Loki, Prometheus, Grafana. Tests via `uv run pytest`.

**Design spec:** `docs/superpowers/specs/2026-06-15-terraria-player-stats-design.md`

**Phase 0 is DONE** — real console grammar captured 2026-06-15:
- join → `DBoy has joined.`
- leave → `DBoy has left.`
- restart → `Listening on port 7777` (also `Server started`)
- deaths/chat → confirmed NOT emitted by vanilla (out of scope)

---

## File Structure

| File | Responsibility |
|------|----------------|
| `ansible/roles/containers/terraria-stats/files/stats.py` | The whole sidecar: pure parser + `StatsState` + SQLite `Store` + Loki client + HTTP server + main loop. |
| `ansible/roles/containers/terraria-stats/files/test_stats.py` | pytest unit tests for the pure logic (parser, state folding, rendering, Loki extraction, Store). |
| `ansible/roles/containers/terraria-stats/templates/docker-compose.yml.j2` | Service definition (image, hardening, healthcheck, kuma label, env, `./data` mount, resources). |
| `ansible/roles/containers/terraria-stats/tasks/main.yml` | Create dirs (incl. `./data`), copy `stats.py`, deploy with `common_config_changed`. |
| `ansible/roles/containers/terraria-stats/meta/deps.yml` | `role_deps: [grafana]` (Loki lives in the grafana role). |
| `ansible/roles/containers/terraria-stats/meta/main.yml` | galaxy_info, `dependencies: []`. |
| `ansible/roles/containers/terraria-stats/CLAUDE.md` | Role doc. |
| `pyproject.toml` | **Modify:** add the new `files` dir to pytest `testpaths`. |
| `ansible/inventory/host_vars/daniel-server.yml` | **Modify:** add `terraria-stats` to `containers_list`. |
| `ansible/roles/containers/prometheus/templates/prometheus.yml.j2` | **Modify:** add `terraria-stats` scrape job. |
| `ansible/roles/containers/grafana/files/dashboards/Terraria/player-stats.json` | Provisioned Grafana dashboard. |
| `ansible/PLANS.md` | **Modify:** record stats feature status. |

---

## Phase 1 — Sidecar logic (TDD)

### Task 1: Register test path + parse_line

**Files:**
- Modify: `pyproject.toml` (testpaths)
- Create: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Add the test directory to pytest testpaths**

In `pyproject.toml`, under `[tool.pytest.ini_options]` `testpaths`, add the line after the existing `monitor-bridge/files` entry:

```toml
  "ansible/roles/containers/terraria-stats/files",
```

- [ ] **Step 2: Write the failing test**

Create `ansible/roles/containers/terraria-stats/files/test_stats.py`:

```python
import stats


def test_parse_join():
    assert stats.parse_line("DBoy has joined.") == ("join", "DBoy")


def test_parse_leave():
    assert stats.parse_line("DBoy has left.") == ("leave", "DBoy")


def test_parse_restart_listening():
    assert stats.parse_line("Listening on port 7777") == ("restart", None)


def test_parse_restart_server_started():
    assert stats.parse_line("Server started") == ("restart", None)


def test_parse_name_with_spaces():
    assert stats.parse_line("Big Boss has joined.") == ("join", "Big Boss")


def test_parse_noise_returns_none():
    for line in (
        "172.21.0.15:59682 is connecting...",
        "Saving world data: 75%",
        "Validating world save: 18%",
        "Backing up world file",
        "",
    ):
        assert stats.parse_line(line) is None
```

- [ ] **Step 3: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'stats'` (and the testpath is now collected).

- [ ] **Step 4: Create stats.py with the module header, env config, and parse_line**

Create `ansible/roles/containers/terraria-stats/files/stats.py`:

```python
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
```

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: parser for join/leave/restart console lines"
```

---

### Task 2: Drift detector (is_unparsed_player_line)

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def test_unparsed_player_line_detects_drift():
    # A future console wording that no longer matches the strict patterns.
    assert stats.is_unparsed_player_line("DBoy has joined the game") is True
    assert stats.is_unparsed_player_line("DBoy has left the world") is True


def test_unparsed_player_line_false_for_valid_and_noise():
    assert stats.is_unparsed_player_line("DBoy has joined.") is False
    assert stats.is_unparsed_player_line("Saving world data: 75%") is False
    assert stats.is_unparsed_player_line("1.2.3.4:5 is connecting...") is False
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k unparsed -v`
Expected: FAIL — `AttributeError: module 'stats' has no attribute 'is_unparsed_player_line'`.

- [ ] **Step 3: Implement** (add to `stats.py` after `parse_line`)

```python
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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k unparsed -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: drift detector for unparsed player lines"
```

---

### Task 3: StatsState — join/leave playtime & sessions

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def test_state_join_then_leave_accrues_playtime():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    p = st.players["DBoy"]
    assert p["total_playtime"] == 60.0
    assert p["sessions"] == 1
    assert p["open_start"] is None
    assert p["first_seen"] == 1000.0
    assert p["last_seen"] == 1060.0


def test_state_first_seen_is_sticky():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    st.apply("join", "DBoy", 2000.0)
    st.apply("leave", "DBoy", 2030.0)
    p = st.players["DBoy"]
    assert p["first_seen"] == 1000.0
    assert p["total_playtime"] == 90.0
    assert p["sessions"] == 2


def test_state_leave_without_join_is_noop():
    st = stats.StatsState()
    st.apply("leave", "Ghost", 1000.0)
    assert "Ghost" not in st.players or st.players["Ghost"]["sessions"] == 0
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k state -v`
Expected: FAIL — `AttributeError: module 'stats' has no attribute 'StatsState'`.

- [ ] **Step 3: Implement** (add to `stats.py` after `is_unparsed_player_line`)

```python
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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k state -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: StatsState join/leave playtime + session folding"
```

---

### Task 4: StatsState — restart, rejoin, online count, live playtime

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def test_restart_closes_open_sessions():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("join", "Pal", 1010.0)
    st.apply("restart", None, 1100.0)
    assert st.players["DBoy"]["total_playtime"] == 100.0
    assert st.players["Pal"]["total_playtime"] == 90.0
    assert st.online_count() == 0


def test_rejoin_without_leave_closes_prior_session():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("join", "DBoy", 1050.0)   # crash/rejoin, no leave
    assert st.players["DBoy"]["sessions"] == 1
    assert st.players["DBoy"]["total_playtime"] == 50.0
    assert st.players["DBoy"]["open_start"] == 1050.0
    assert st.online_count() == 1


def test_live_playtime_includes_open_session():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    assert st.playtime("DBoy", now=1040.0) == 40.0   # 0 closed + 40 live
    st.apply("leave", "DBoy", 1100.0)
    assert st.playtime("DBoy", now=9999.0) == 100.0  # closed, no live delta
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k "restart or rejoin or live_playtime" -v`
Expected: FAIL — `AttributeError: 'StatsState' object has no attribute 'online_count'`.

- [ ] **Step 3: Implement** (add these methods to `StatsState` in `stats.py`)

```python
    def online_count(self):
        return sum(1 for p in self.players.values() if p["open_start"] is not None)

    def playtime(self, name, now):
        """Total playtime incl. the in-progress session so the counter ticks live."""
        p = self.players[name]
        base = p["total_playtime"]
        if p["open_start"] is not None:
            base += max(0.0, now - p["open_start"])
        return base
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k "restart or rejoin or live_playtime" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: restart/rejoin handling + online count + live playtime"
```

---

### Task 5: Prometheus rendering

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def test_escape_label_value():
    assert stats.escape_label_value('a"b\\c') == 'a\\"b\\\\c'


def test_render_metrics_contains_expected_series():
    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1060.0)
    st.apply("join", "Pal", 2000.0)
    st.unmatched = 2
    out = stats.render_metrics(st, now=2030.0)
    assert 'terraria_player_playtime_seconds_total{player="DBoy"} 60' in out
    assert 'terraria_player_playtime_seconds_total{player="Pal"} 30' in out
    assert 'terraria_player_sessions_total{player="DBoy"} 1' in out
    assert "terraria_players_online 1" in out
    assert "terraria_stats_unmatched_player_lines_total 2" in out
    assert "# TYPE terraria_players_online gauge" in out
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k "escape or render" -v`
Expected: FAIL — `AttributeError: module 'stats' has no attribute 'escape_label_value'`.

- [ ] **Step 3: Implement** (add to `stats.py` after the `StatsState` class)

```python
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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k "escape or render" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: Prometheus metrics rendering"
```

---

### Task 6: Loki extraction + apply_entries

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def _loki_response(entries):
    # entries: list of (ts_ns_int, line). Mimics Loki query_range JSON.
    return {"data": {"result": [
        {"stream": {"container": "terraria"},
         "values": [[str(ts), line] for ts, line in entries]}
    ]}}


def test_extract_entries_sorts_ascending():
    resp = _loki_response([(300, "c"), (100, "a"), (200, "b")])
    assert stats.extract_entries(resp) == [(100, "a"), (200, "b"), (300, "c")]


def test_extract_entries_empty():
    assert stats.extract_entries({"data": {"result": []}}) == []


def test_apply_entries_folds_and_counts_unmatched():
    st = stats.StatsState()
    entries = [
        (1_000_000_000, "DBoy has joined."),
        (5_000_000_000, "DBoy has left."),          # +4s
        (6_000_000_000, "DBoy has joined the game"),  # drift -> unmatched
        (7_000_000_000, "Saving world data: 5%"),     # noise -> ignored
    ]
    evs, maxts = stats.apply_entries(st, entries)
    assert st.players["DBoy"]["total_playtime"] == 4.0
    assert st.players["DBoy"]["sessions"] == 1
    assert st.unmatched == 1
    assert maxts == 7_000_000_000
    assert evs == [
        (1_000_000_000, "DBoy", "join", "DBoy has joined."),
        (5_000_000_000, "DBoy", "leave", "DBoy has left."),
    ]
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k "extract or apply_entries" -v`
Expected: FAIL — `AttributeError: module 'stats' has no attribute 'extract_entries'`.

- [ ] **Step 3: Implement** (add to `stats.py` after `render_metrics`)

```python
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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k "extract or apply_entries" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: Loki extraction + apply_entries event folding"
```

---

### Task 7: SQLite Store (persistence + cursor)

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def test_store_roundtrip_and_cursor(tmp_path):
    db = str(tmp_path / "stats.db")
    store = stats.Store(db)
    assert store.get_cursor() == 0

    st = stats.StatsState()
    st.apply("join", "DBoy", 1000.0)
    st.apply("leave", "DBoy", 1100.0)
    store.append_events([(1_100_000_000_000, "DBoy", "leave", "DBoy has left.")])
    store.save(st, cursor_ns=1_100_000_000_000)

    # Reopen: state + cursor survive (durable source of truth).
    store2 = stats.Store(db)
    assert store2.get_cursor() == 1_100_000_000_000
    loaded = store2.load_state()
    assert loaded.players["DBoy"]["total_playtime"] == 100.0
    assert loaded.players["DBoy"]["sessions"] == 1


def test_store_preserves_open_session(tmp_path):
    db = str(tmp_path / "stats.db")
    store = stats.Store(db)
    st = stats.StatsState()
    st.apply("join", "DBoy", 2000.0)     # still online
    store.save(st, cursor_ns=2_000_000_000_000)
    loaded = stats.Store(db).load_state()
    assert loaded.players["DBoy"]["open_start"] == 2000.0
    assert loaded.online_count() == 1
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k store -v`
Expected: FAIL — `AttributeError: module 'stats' has no attribute 'Store'`.

- [ ] **Step 3: Implement** (add to `stats.py` after `apply_entries`)

```python
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
            if ls:
                st.last_event_ts = max(st.last_event_ts, ls)
        return st

    def get_cursor(self):
        row = self.conn.execute("SELECT last_ts_ns FROM cursor WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def append_events(self, events):
        if events:
            self.conn.executemany(
                "INSERT INTO events(ts_ns,player,kind,raw) VALUES(?,?,?,?)", events)
            self.conn.commit()

    def save(self, state, cursor_ns):
        """Persist player snapshot + cursor atomically (single transaction)."""
        c = self.conn
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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k store -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: SQLite Store (durable state + cursor)"
```

---

### Task 8: Loki client, HTTP server, main loop (wiring)

**Files:**
- Modify: `ansible/roles/containers/terraria-stats/files/stats.py`
- Test: `ansible/roles/containers/terraria-stats/files/test_stats.py`

- [ ] **Step 1: Write the failing test** (append to `test_stats.py`)

```python
def test_run_cycle_end_to_end(tmp_path):
    db = str(tmp_path / "stats.db")
    store = stats.Store(db)
    state = store.load_state()
    pages = [[
        (1_000_000_000, "DBoy has joined."),
        (4_000_000_000, "DBoy has left."),
    ]]

    def fake_fetch(start_ns, end_ns):
        # one page then empty (mimics pagination end)
        return pages.pop(0) if pages else []

    cursor = stats.run_cycle(state, store, cursor=0,
                             end_ns=9_000_000_000, fetch=fake_fetch)
    assert cursor == 4_000_000_000
    assert state.players["DBoy"]["total_playtime"] == 3.0
    # persisted
    assert stats.Store(db).get_cursor() == 4_000_000_000
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -k run_cycle -v`
Expected: FAIL — `AttributeError: module 'stats' has no attribute 'run_cycle'`.

- [ ] **Step 3: Implement** (add to `stats.py` after the `Store` class)

```python
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
        store.append_events(events)
        if max_ts > cursor:
            cursor = max_ts
        store.save(state, cursor)
        if len(entries) < LOKI_PAGE_LIMIT:
            break
    return cursor


def log(*args):
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


_state = StatsState()
_lock = threading.Lock()
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


def main():
    global _state, _last_poll_ok
    once = "--once" in sys.argv
    backfill = "--backfill" in sys.argv
    store = Store(DB_PATH)
    with _lock:
        _state = store.load_state()
    cursor = 0 if backfill else store.get_cursor()
    log("terraria-stats starting (loki=%s once=%s backfill=%s players=%d)"
        % (LOKI_URL, once, backfill, len(_state.players)))
    if not (once or backfill):
        threading.Thread(target=lambda: HTTPServer(
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
```

> Note: `run_cycle` is tested with a `fetch` that ignores its args and returns a list, which exercises the fold + persistence path without HTTP. `loki_fetch` (the real I/O) is smoke-tested live in Task 14.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest ansible/roles/containers/terraria-stats/files -v`
Expected: PASS (all tests from Tasks 1–8).

- [ ] **Step 5: Commit**

```bash
git add ansible/roles/containers/terraria-stats/files/stats.py ansible/roles/containers/terraria-stats/files/test_stats.py
git commit -m "terraria-stats: Loki client, HTTP /metrics+/healthz, main loop"
```

---

## Phase 2 — Ansible role & deploy

### Task 9: Role metadata + tasks

**Files:**
- Create: `ansible/roles/containers/terraria-stats/meta/main.yml`
- Create: `ansible/roles/containers/terraria-stats/meta/deps.yml`
- Create: `ansible/roles/containers/terraria-stats/tasks/main.yml`

- [ ] **Step 1: Create `meta/main.yml`**

```yaml
---
galaxy_info:
  author: DanielH2018
  description: "Terraria player playtime/presence stats (Loki -> SQLite -> Prometheus)"
  license: MIT
  min_ansible_version: "2.15"

# Logical ordering: needs Loki (grafana role) up first to read from. Actual sequencing is
# computed by the toposort filter in deploy.yml reading meta/deps.yml.
dependencies: []
```

- [ ] **Step 2: Create `meta/deps.yml`**

```yaml
---
# Deploy after grafana (which ships Loki) so the log source is up; Prometheus scrapes
# this service but tolerates an absent target, so it is not an ordering dependency.
role_deps:
  - grafana
```

- [ ] **Step 3: Create `tasks/main.yml`**

```yaml
---
- name: Create required directories
  tags: [config]
  ansible.builtin.include_role:
    name: common
    tasks_from: setup_dirs.yml
  vars:
    common_dirs_to_create:
      - "{{ container_item.name }}"
      # ./data holds the SQLite DB; create it owned by the deploy user so the non-root
      # container can write it (else Docker auto-creates the bind-mount root-owned).
      - "{{ container_item.name }}/data"

- name: Deploy terraria-stats script
  tags: [config]
  ansible.builtin.copy:
    src: stats.py
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/stats.py"
    mode: "0644"
  register: terraria_stats_script

- name: Deploy Container
  tags: [deploy]
  ansible.builtin.include_role:
    name: common
    tasks_from: docker_deploy.yml
  vars:
    # stats.py is bind-mounted and read once at startup, so only a recreate applies a
    # code change. Without this a script-only edit leaves recreate: auto and never deploys.
    common_config_changed: "{{ terraria_stats_script is changed }}"
```

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/terraria-stats/meta ansible/roles/containers/terraria-stats/tasks
git commit -m "terraria-stats: ansible role metadata + tasks"
```

---

### Task 10: Compose template

**Files:**
- Create: `ansible/roles/containers/terraria-stats/templates/docker-compose.yml.j2`

- [ ] **Step 1: Create the compose template**

```jinja
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'healthcheck.yml.j2' import healthcheck %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
---

services:
  terraria-stats:
    image: python:3.14-alpine
    container_name: terraria-stats
    user: "{{ puid }}:{{ pgid }}"
    restart: unless-stopped
    # Pure stdlib Python; only ./data (SQLite) needs to be writable.
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    command: ["python", "/app/stats.py"]
    # /healthz returns 503 once the last successful Loki poll is older than ~3x the poll
    # interval, so autoheal restarts a hung loop (a crash already exits the container).
    {{ healthcheck("[\"CMD\", \"python3\", \"-c\", \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9420/healthz',timeout=5).status==200 else 1)\"]") }}
    environment:
      - TZ={{ tz }}
      - PYTHONUNBUFFERED=1
      - PYTHONDONTWRITEBYTECODE=1
      - LOKI_URL=http://loki:3100
      # Quote: the value starts with '{', a YAML flow-mapping indicator. (stats.py's
      # default selector is identical, so this is for discoverability/tunability.)
      - 'LOKI_QUERY={container="terraria"}'
      - POLL_INTERVAL=20
      - METRICS_PORT=9420
      - DB_PATH=/data/stats.db
    volumes:
      - ./stats.py:/app/stats.py:ro
      - ./data:/data
    {{ service_networks() }}
    labels:
      # Container-liveness monitor; the actual stats are scraped by Prometheus + shown
      # in Grafana, so no push monitor here.
      {{ kuma('terraria-stats') }}
    # Resource caps for blast-radius containment (M1); tune from cAdvisor/Grafana.
    {{ resources('0.50', '128M', '0.05', '32M') }}

{{ external_networks() }}
```

- [ ] **Step 2: Validate the rendered template**

Run: `uv run python scripts/validate_compose_templates.py 2>&1 | grep -E "terraria-stats|failure"`
Expected: `[ok]   terraria-stats` and `0 failure(s)`.

> If it reports `terraria-stats` not found, that's expected until Task 11 registers it in `containers_list` — re-run this step's validation after Task 11.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/terraria-stats/templates/docker-compose.yml.j2
git commit -m "terraria-stats: docker-compose template"
```

---

### Task 11: Register in containers_list

**Files:**
- Modify: `ansible/inventory/host_vars/daniel-server.yml`

- [ ] **Step 1: Add the entry** next to the `terraria` block (after its `networks:` list, under the "Game servers" section)

```yaml
  - name: terraria-stats
    # Headless sidecar: reads terraria console lines from Loki, serves Prometheus
    # metrics on :9420. No web UI -> no port/Authelia. monitoring net = reach Loki
    # and be scraped by Prometheus.
    port: false
    use_authelia: false
    networks:
      - monitoring
```

- [ ] **Step 2: Validate render + deploy ordering**

Run: `uv run python scripts/validate_compose_templates.py 2>&1 | grep -E "terraria-stats|failure"`
Expected: `[ok]   terraria-stats`, `0 failure(s)`.

Run: `uv run pytest ansible/tests -q`
Expected: PASS (toposort ordering still valid with the new role + its `grafana` dep).

- [ ] **Step 3: Commit**

```bash
git add ansible/inventory/host_vars/daniel-server.yml
git commit -m "terraria-stats: register in daniel-server containers_list"
```

---

### Task 12: Prometheus scrape job

**Files:**
- Modify: `ansible/roles/containers/prometheus/templates/prometheus.yml.j2`

- [ ] **Step 1: Add the scrape job** at the end of `scrape_configs` (after the `crowdsec_daniel-server` block, before the final separator line)

```yaml
  - job_name: "terraria-stats"
    scrape_interval: 1m
    static_configs:
    - targets: ["terraria-stats:9420"]

#################################################################################################
```

- [ ] **Step 2: Sanity-check YAML renders**

Run: `uv run python scripts/validate_compose_templates.py 2>&1 | grep -E "prometheus|failure"`
Expected: `[ok]   prometheus`, `0 failure(s)`.

- [ ] **Step 3: Commit**

```bash
git add ansible/roles/containers/prometheus/templates/prometheus.yml.j2
git commit -m "terraria-stats: add prometheus scrape job"
```

---

### Task 13: Role CLAUDE.md

**Files:**
- Create: `ansible/roles/containers/terraria-stats/CLAUDE.md`

- [ ] **Step 1: Create the doc**

```markdown
# terraria-stats — Terraria player playtime/presence stats

Headless sidecar that turns the Terraria console (via Loki) into all-time per-player
playtime/session/presence metrics for Grafana. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `python:3.14-alpine` (stdlib only — no build, no deps)
- **Host:** daniel-server · **No web UI**, no Authelia · **Metrics:** `:9420/metrics`
- **Networks:** `monitoring` (read `loki:3100`, scraped by Prometheus)
- **Depends on:** grafana (ships Loki) — `meta/deps.yml`
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Reads from Loki, never the docker socket and never the terraria container.** Polls
  `query_range` for `{container="terraria"}` every `POLL_INTERVAL` (20s), cursor-based.
- **Deaths are NOT tracked** — the vanilla console only emits `has joined`/`has left`
  (verified Phase 0, 2026-06-15); deaths/chat never reach it. Deaths would require a
  TShock+SSC migration (rejected). Do not re-add a death metric without that.
- `files/stats.py` is a **static** stdlib script (env-driven). SQLite (`./data/stats.db`,
  Kopia-backed) is the durable source of truth; Prometheus/Grafana are the display layer.
- Metrics: `terraria_player_playtime_seconds_total{player}` (incl. live session),
  `terraria_player_sessions_total{player}`, `terraria_players_online`,
  `terraria_stats_last_event_timestamp`, `terraria_stats_unmatched_player_lines_total`.
- A server restart closes all open sessions (players drop with no "has left"). The
  unmatched-lines counter is the safety net for future console-wording drift.

## Editing & testing
- Compose: `templates/docker-compose.yml.j2` · Logic: `files/stats.py`
- Unit tests: `uv run pytest ansible/roles/containers/terraria-stats/files`
- Smoke (one pass, no server): `docker exec terraria-stats python /app/stats.py --once`
- Rebuild all-time state from Loki history: `docker exec terraria-stats python /app/stats.py --backfill`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "terraria-stats"`
```

- [ ] **Step 2: Commit**

```bash
git add ansible/roles/containers/terraria-stats/CLAUDE.md
git commit -m "terraria-stats: role CLAUDE.md"
```

---

### Task 14: Pre-commit gate + deploy + verify

**Files:** none (verification task)

- [ ] **Step 1: Run the full pre-commit gate**

Run: `prek run --all-files`
Expected: all hooks Passed (ansible-lint, gitleaks, validate-compose-templates, pytest incl. the new `terraria-stats/files` suite).

- [ ] **Step 2: Dry-run deploy**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "terraria-stats" --check`
Expected: `failed=0`; the compose + script tasks show `changed`.

- [ ] **Step 3: Deploy for real**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "terraria-stats"`
Expected: `failed=0`, `changed>=1`.

- [ ] **Step 4: Backfill all-time state from Loki history, then verify metrics**

Run:
```bash
docker exec terraria-stats python /app/stats.py --backfill
docker exec terraria-stats python /app/stats.py --once
curl -s http://localhost:9420/metrics | grep terraria_player || \
  docker exec terraria-stats python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9420/metrics').read().decode())" | grep terraria_
```
Expected: `terraria_player_playtime_seconds_total{player="DBoy"} N` present (the 2026-06-15 join/leave cycles produce playtime > 0), `terraria_players_online 0`.

- [ ] **Step 5: Confirm Prometheus is scraping and health is green**

Run:
```bash
docker inspect terraria-stats --format 'Health: {{.State.Health.Status}}'
uv run ansible-playbook ansible/deploy.yml --tags "prometheus"
```
Then in the Prometheus UI (`prometheus.<domain>` → Status → Targets) confirm the `terraria-stats` target is **UP**.
Expected: container `healthy`; target UP.

- [ ] **Step 6: Commit** (only if the deploy required any template tweaks; otherwise nothing to commit)

```bash
git add -A && git commit -m "terraria-stats: deploy fixes" || echo "nothing to commit"
```

---

## Phase 3 — Grafana dashboard

### Task 15: Provision the Terraria dashboard

**Files:**
- Create: `ansible/roles/containers/grafana/files/dashboards/Terraria/player-stats.json`

- [ ] **Step 1: Create the dashboard JSON** (datasource uid pinned to Prometheus `EGdsQqhVk`; folder = `Terraria`)

```json
{
  "uid": "terraria-player-stats",
  "title": "Terraria — Player Stats",
  "tags": ["terraria"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "30s",
  "time": { "from": "now-30d", "to": "now" },
  "panels": [
    {
      "id": 1,
      "type": "stat",
      "title": "Players Online",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 6, "w": 6, "x": 0, "y": 0 },
      "targets": [
        { "refId": "A", "expr": "terraria_players_online", "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ]
    },
    {
      "id": 2,
      "type": "table",
      "title": "Leaderboard",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 10, "w": 18, "x": 6, "y": 0 },
      "fieldConfig": { "defaults": { "custom": {} }, "overrides": [
        { "matcher": { "id": "byName", "options": "Playtime" },
          "properties": [ { "id": "unit", "value": "s" } ] }
      ] },
      "transformations": [
        { "id": "joinByField", "options": { "byField": "player", "mode": "outer" } },
        { "id": "organize", "options": {
          "renameByName": {
            "player": "Player",
            "Value #Playtime": "Playtime",
            "Value #Sessions": "Sessions"
          },
          "excludeByName": { "Time": true, "Time 1": true, "Time 2": true,
            "__name__": true, "__name__ 1": true, "__name__ 2": true,
            "job": true, "job 1": true, "job 2": true,
            "instance": true, "instance 1": true, "instance 2": true } } }
      ],
      "targets": [
        { "refId": "Playtime", "format": "table", "instant": true,
          "expr": "terraria_player_playtime_seconds_total",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } },
        { "refId": "Sessions", "format": "table", "instant": true,
          "expr": "terraria_player_sessions_total",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ]
    },
    {
      "id": 3,
      "type": "timeseries",
      "title": "Playtime Accrual (per player)",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "fieldConfig": { "defaults": { "unit": "s" }, "overrides": [] },
      "gridPos": { "h": 9, "w": 12, "x": 0, "y": 10 },
      "targets": [
        { "refId": "A", "expr": "terraria_player_playtime_seconds_total",
          "legendFormat": "{{player}}",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ]
    },
    {
      "id": 4,
      "type": "timeseries",
      "title": "Players Online (over time)",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 9, "w": 6, "x": 12, "y": 10 },
      "targets": [
        { "refId": "A", "expr": "terraria_players_online",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ]
    },
    {
      "id": 5,
      "type": "timeseries",
      "title": "Parser Health (unmatched lines/min)",
      "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" },
      "gridPos": { "h": 9, "w": 6, "x": 18, "y": 10 },
      "targets": [
        { "refId": "A", "expr": "rate(terraria_stats_unmatched_player_lines_total[5m]) * 60",
          "datasource": { "type": "prometheus", "uid": "EGdsQqhVk" } }
      ]
    }
  ]
}
```

- [ ] **Step 2: Validate the JSON parses**

Run: `python3 -m json.tool ansible/roles/containers/grafana/files/dashboards/Terraria/player-stats.json > /dev/null && echo OK`
Expected: `OK`.

- [ ] **Step 3: Deploy grafana and verify the dashboard**

Run: `uv run ansible-playbook ansible/deploy.yml --tags "grafana"`
Expected: `failed=0`.
Then in Grafana (`grafana.<domain>`) open the **Terraria** folder → **Terraria — Player Stats**; confirm the leaderboard shows `DBoy` with non-zero playtime and the panels render.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/containers/grafana/files/dashboards/Terraria/player-stats.json
git commit -m "terraria-stats: Grafana player-stats dashboard"
```

---

## Phase 4 — Docs

### Task 16: Update PLANS.md and spec status

**Files:**
- Modify: `ansible/PLANS.md`
- Modify: `docs/superpowers/specs/2026-06-15-terraria-player-stats-design.md`

- [ ] **Step 1: Move the stats item to Superseded in `ansible/PLANS.md`**

Replace the "Player stats for Terraria" Backlog bullet with a Superseded entry:

```markdown
- Player stats for Terraria — done 2026-06-15: shipped the `terraria-stats` sidecar
  (Loki → SQLite → Prometheus → Grafana) tracking all-time playtime/sessions/presence.
  Deaths were dropped: Phase 0 LAN capture proved the vanilla console emits only
  `has joined`/`has left` (no deaths/chat), and deaths would need a TShock+SSC
  migration (evaluated, rejected). Spec + plan under docs/superpowers/.
```

- [ ] **Step 2: Flip the spec status line** in `docs/superpowers/specs/2026-06-15-terraria-player-stats-design.md`

```markdown
**Status:** Implemented
```

- [ ] **Step 3: Commit**

```bash
git add ansible/PLANS.md docs/superpowers/specs/2026-06-15-terraria-player-stats-design.md
git commit -m "terraria-stats: mark feature done in PLANS + spec"
```

---

## Done criteria

- `uv run pytest ansible/roles/containers/terraria-stats/files` green; `prek run --all-files` green.
- `terraria-stats` deployed, container `healthy`, Prometheus target UP.
- Grafana "Terraria — Player Stats" shows `DBoy` with playtime from the 2026-06-15 sessions.
- Terraria container untouched throughout (unchanged image/compose/world).
