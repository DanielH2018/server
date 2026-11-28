#!/usr/bin/env python3
"""stats_lib — shared Loki-tail -> Prometheus-exporter skeleton for the game-stats roles.

Shipped as a sibling copy beside valheim_stats.py and stats.py (terraria-stats), the same
mechanism `hosts/lib/host_lib.py` uses: a directly-invoked script gets only its own directory
on `sys.path`, so a shared module has to be copied in rather than imported across the tree (see `.claude/rules/python-layout.md`). This one ships into a ConfigMap and
runs inside a python:3.15-alpine pod rather than on a host, so each game's rendered ConfigMap
(`k8s/roles/game-stats/templates/configmap-*.yaml.j2`) carries a copy beside the entry
script, rather than a host package shipping one beside a host script.

Both stats roles tail a game's console out of Loki, parse join/leave-shaped lines, fold them
into all-time per-player stats kept in SQLite, and serve Prometheus metrics. They were built
as two independent forks and diverged only in the ~250 lines that genuinely differ per game:
the line grammar (Valheim's console differs from Terraria's, not as a dialect but as a
different language — see valheim_stats.py's own docstring), the state machine each grammar
needs (Valheim tracks deaths and a SteamID<->name map; Terraria does not), and the SQLite
schema that persists that state. Those three stay in each role's own file. A game's `Store`
is only that schema plus its `load_state`/`write_state`: the connection lifecycle, the `save`
transaction and the two game-independent tables are `SqliteStore` here, which each game's
`Store` subclasses.

What lives here instead — the mechanical skeleton neither game's identity touches:
  - the env reader (`env`)
  - Loki fetch (`http_get_json`, `build_query_range_url`, `extract_entries`)
  - cursor handling (`initial_cursor`, `run_cycle`)
  - metric rendering/escaping (`escape_label_value`, `render_family`)
  - the HTTP handler (`PollState`, `make_handler`, `start_metrics_server`)
  - the run loop (`poll_forever`) and the entry point both games' `main()` is
    (`RunConfig`, `run`)
  - the SQLite connection lifecycle and the `cursor`/`events` tables (`SqliteStore`)
  - a timestamped logger (`log`)

Every function here is stdlib-only and takes its game-specific bits (URLs, the per-game
`apply_fn`, the per-game `render_metrics`/`on_poll_ok` callables) as arguments rather than
reading a module-level constant, so this module carries no per-game state of its own and
needs no test doubles patched onto it — a caller passes a fake `fetch`/`apply_fn` instead.
"""

import argparse
import copy
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env(name, default):
    return os.environ.get(name, default)


def log(*args):
    print("[%s]" % time.strftime("%Y-%m-%dT%H:%M:%S"), *args, flush=True)


# Loki ingestion
def http_get_json(url, timeout, user_agent):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# DECIDED: this builder stays here rather than routing through
# `scripts/diagnostics/probe_lib/obs_api.py`, the shared Prometheus/Loki client. Three reasons,
# in order of weight.
#
# The window semantics differ, and that is the part a shared builder would have to express. This
# one is a CURSOR resume over the half-open interval `(start_ns, end_ns]` — hence `start_ns + 1`,
# which is what keeps the last entry of the previous page from being re-folded into the stats. A
# shared `loki_query_url` passes `start` through verbatim, correct for its own callers and off by
# one entry for this one; `obs_api.trailing_window_ns` is inapplicable outright, because nothing
# here reads a trailing window — the start comes out of SQLite (`initial_cursor`).
#
# The cost side is a staging path, not a line count. obs_api's own docstring says an edit there
# maps to no deploy tag: homelab-mcp's image keeps the previous copy until its next deploy. Two
# more pods on that footing, each with a third file embedded in its ConfigMap, is the real
# bill — and a stale obs_api in a game pod is a stats gap nobody is watching for.
#
# What would be shared is one `urlencode` call. The transports also differ: `http_get_json` here
# sends a User-Agent, which `obs_api.get_json` does not.
#
# Contradict this with evidence at a cited file:line, naming this marker. The thing that would
# overturn it is obs_api growing a cursor-shaped window with a deploy tag behind it.
def build_query_range_url(loki_url, query, start_ns, end_ns, page_limit):
    """Builds a Loki query_range URL for entries in (start_ns, end_ns], one page."""
    qs = urllib.parse.urlencode(
        {
            "query": query,
            "start": start_ns + 1,
            "end": end_ns,
            "limit": page_limit,
            "direction": "forward",
        }
    )
    return loki_url.rstrip("/") + "/loki/api/v1/query_range?" + qs


# DECIDED: a copy of monitor-bridge's envelope rule (`bridge/net.py:_query_result`), not an
# import: each game's ConfigMap carries only its script and this file.
def _loki_streams(loki_json):
    """The stream objects of a Loki answer; an absent `data` or `result` reads as none.

    Raises:
        ValueError: The answer, its `data` or a stream is not an object, or `result` is not a
            list. A `"data": null` raises rather than reading as no entries.
    """
    if not isinstance(loki_json, dict):
        raise ValueError(f"loki answer is {type(loki_json).__name__}, not an object")
    data = loki_json.get("data", {})
    if not isinstance(data, dict):
        raise ValueError(f"loki answer's data is {type(data).__name__}, not an object")
    streams = data.get("result", [])
    if not isinstance(streams, list) or not all(isinstance(s, dict) for s in streams):
        raise ValueError("loki answer's data.result is not a list of objects")
    return streams


def extract_entries(loki_json):
    """Flatten a Loki query_range response to [(ts_ns:int, line:str)] ascending.

    Raises:
        ValueError: The response is not a Loki streams answer, `"data": null` included.
    """
    out = []
    for stream in _loki_streams(loki_json):
        for ts, line in stream.get("values", []):
            out.append((int(ts), line))
    out.sort(key=lambda tl: tl[0])
    return out


# cursor handling

# Loki's default `max_entries_limit_per_query`, which loki-homelab does not raise.
LOKI_MAX_ENTRIES = 5000


def initial_cursor(stored_cursor, backfill, now, backfill_days):
    """Pick the starting cursor (ns).

    On a fresh DB (stored_cursor==0) or an explicit --backfill, bound the start to the last
    `backfill_days` rather than epoch: a first query spanning 1970->now exceeds Loki's
    max_query_length (~30d) and returns HTTP 400. A normal run resumes from the cursor.
    """
    if backfill or stored_cursor == 0:
        return int((now - backfill_days * 86400) * 1e9)
    return stored_cursor


def run_cycle(state, store, cursor, end_ns, fetch, apply_fn, page_limit):
    """One poll: page through new entries from `cursor`, fold, persist. Returns new cursor.

    `fetch(start_ns, end_ns, limit) -> [(ts_ns, line)]` returns the earliest `limit`
    entries, ascending. `apply_fn(state, entries) -> (events, max_ts)` is the per-game
    fold (e.g. Valheim's excludes heartbeats from the audit log, Terraria's has no
    heartbeat at all). Pages until a short/empty page. State mutation + cursor advance are
    persisted together so a crash re-runs the batch cleanly (events past the saved cursor
    simply re-apply on next start). A page whose fold or save raises leaves `state` as it
    was before the page, so the retry that resumes from the stored cursor folds it once.

    A full page may stop partway through its last timestamp, and the cursor cannot point
    inside one, so those entries wait for the next page. A full page holding a single
    timestamp is fetched again with twice the limit, up to LOKI_MAX_ENTRIES. A page
    still holding one timestamp there is folded and logged, and any further entries at
    that timestamp are skipped: Loki serves no larger page.
    """
    limit = page_limit
    while True:
        entries = fetch(cursor, end_ns, limit)
        if not entries:
            break
        full = len(entries) >= limit
        if full:
            last_ts = entries[-1][0]
            earlier = [entry for entry in entries if entry[0] < last_ts]
            if earlier:
                entries = earlier
            elif limit < LOKI_MAX_ENTRIES:
                limit = min(limit * 2, LOKI_MAX_ENTRIES)
                continue
            else:
                log(
                    "page of %d entries at ts %d: any more are skipped"
                    % (limit, last_ts)
                )
        before = copy.deepcopy(vars(state))
        try:
            events, max_ts = apply_fn(state, entries)
            page_cursor = max(cursor, max_ts)
            store.save(state, page_cursor, events)
        except BaseException:
            vars(state).clear()
            vars(state).update(before)
            raise
        cursor = page_cursor
        if not full:
            break
        limit = page_limit
    return cursor


# Prometheus exposition
def escape_label_value(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_family(lines, name, help_text, metric_type, samples, label_name=None):
    """Appends one Prometheus metric family (HELP + TYPE + one line per sample) to `lines`.

    `samples` is [(label_value, value)] pairs when `label_name` is given — a per-player
    metric, already sorted by the caller so player order in the output is deterministic —
    or a single-element [value] for a label-less scalar (a player count, a timestamp, an
    unmatched-line total).
    """
    lines.append("# HELP %s %s" % (name, help_text))
    lines.append("# TYPE %s %s" % (name, metric_type))
    if label_name is None:
        (value,) = samples
        lines.append("%s %d" % (name, value))
        return
    for label_value, value in samples:
        lines.append(
            '%s{%s="%s"} %d'
            % (name, label_name, escape_label_value(label_value), value)
        )


# HTTP serving + the run loop
class PollState:
    """Mutable state the poll loop and the HTTP handler share.

    `value` is the per-game StatsState (opaque to this module); `lock` guards it for
    /metrics rendering; `last_poll_ok` is a Unix timestamp the loop stamps after every
    successful cycle and /healthz reads without the lock (float assignment is atomic under
    CPython's GIL — guard it with `lock` too if this ever runs on a free-threaded
    interpreter).
    """

    def __init__(self, initial_state):
        self.value = initial_state
        self.lock = threading.Lock()
        self.last_poll_ok = 0.0


def make_handler(poll_state, render_metrics_fn, health_max_age, clock=time.time):
    """Builds a BaseHTTPRequestHandler serving /metrics and /healthz.

    `render_metrics_fn(state, now) -> str` is the per-game exposition renderer. `clock` is
    what the handler reads for `now` on every request; a test passes a constant so the
    /healthz staleness verdict is a fixed distance from `last_poll_ok`.
    """

    class Handler(BaseHTTPRequestHandler):
        """Serves /metrics (Prometheus exposition) and /healthz (poll staleness)."""

        # `format` is the parameter name BaseHTTPRequestHandler.log_message declares.
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self):
            """Routes the request path to /metrics, /healthz, or a 404."""
            if self.path == "/metrics":
                with poll_state.lock:
                    body = render_metrics_fn(poll_state.value, clock()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/healthz":
                fresh = (clock() - poll_state.last_poll_ok) < health_max_age
                self.send_response(200 if fresh else 503)
                self.end_headers()
                self.wfile.write(b"ok\n" if fresh else b"stale\n")
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def start_metrics_server(handler_cls, port):
    """Starts the /metrics + /healthz server on a daemon thread.

    Threading so a slow /metrics render cannot head-of-line-block /healthz.
    """
    threading.Thread(
        target=lambda: ThreadingHTTPServer(
            ("0.0.0.0", port), handler_cls
        ).serve_forever(),
        daemon=True,
    ).start()


def poll_forever(
    poll_state,
    store,
    cursor,
    fetch,
    apply_fn,
    page_limit,
    once,
    backfill,
    poll_interval,
    on_poll_ok,
):
    """Runs run_cycle in a loop, persisting the cursor and stamping poll_state.last_poll_ok.

    Returns after one cycle when `once`/`backfill`; otherwise loops forever at
    `poll_interval`. A cycle's own exception is caught and logged so an unreachable Loki
    cannot kill the loop, and the next cycle resumes after the last page the failed one
    saved. `on_poll_ok(state) -> str` describes a successful cycle for the
    log line — the one per-game difference left in the loop (Valheim logs a death count,
    Terraria does not).
    """
    while True:
        try:
            end_ns = int(time.time() * 1e9)
            with poll_state.lock:
                cursor = run_cycle(
                    poll_state.value, store, cursor, end_ns, fetch, apply_fn, page_limit
                )
            poll_state.last_poll_ok = time.time()
            log("poll ok: " + on_poll_ok(poll_state.value))
        except Exception as e:  # an unreachable Loki must not kill the loop
            log("poll error:", e)
            # The failed cycle may have saved pages that its lost return value covered.
            cursor = max(cursor, store.get_cursor())
        if once or backfill:
            return
        time.sleep(poll_interval)


# SQLite lifecycle — the half of each game's Store that is not its schema
class SqliteStore:
    """Connection lifecycle + the game-independent tables of a stats Store.

    A subclass owns what genuinely differs per game: the `players` schema (Valheim carries a
    death count and a SteamID<->name map, Terraria neither), `load_state`, and the rows
    `write_state` adds to `save`'s transaction. It
    declares its own tables in `_init_game_schema`, which this constructor calls before the
    single commit — so a subclass hook must not read an attribute assigned after
    `super().__init__()`.

    `cursor` and `events` are created here because both games ingest the same way: one
    single-row ingest cursor and one raw audit log.
    """

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
        c.execute("""CREATE TABLE IF NOT EXISTS cursor(
            id INTEGER PRIMARY KEY CHECK(id=1), last_ts_ns INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            ts_ns INTEGER, player TEXT, kind TEXT, raw TEXT)""")
        self._init_game_schema(c)
        c.commit()

    def _init_game_schema(self, conn):
        """Declares the per-game tables. Subclasses override; no commit, the caller commits."""
        raise NotImplementedError

    def get_cursor(self):
        row = self.conn.execute("SELECT last_ts_ns FROM cursor WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def save(self, state, cursor_ns, events=()):
        """Persists events, the game's rows and the cursor as one transaction.

        A failure rolls the whole transaction back, so no row it wrote reaches a later commit.
        """
        with self.conn as c:
            if events:
                c.executemany(
                    "INSERT INTO events(ts_ns,player,kind,raw) VALUES(?,?,?,?)", events
                )
            self.write_state(c, state)
            c.execute(
                "INSERT INTO cursor(id,last_ts_ns) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET last_ts_ns=excluded.last_ts_ns",
                (cursor_ns,),
            )

    def write_state(self, conn, state):
        """Writes the game's rows for `state`. Subclasses override; no commit, `save` commits."""
        raise NotImplementedError


# The entry point both roles' main() is
class RunConfig:
    """The tunables `run` reads, one instance per game built from that game's env constants."""

    def __init__(
        self,
        service_name,
        loki_url,
        backfill_days,
        page_limit,
        poll_interval,
        metrics_port,
        health_max_age,
    ):
        self.service_name = service_name
        self.loki_url = loki_url
        self.backfill_days = backfill_days
        self.page_limit = page_limit
        self.poll_interval = poll_interval
        self.metrics_port = metrics_port
        self.health_max_age = health_max_age


def parse_args(argv):
    """Parses the exporter's flags, exiting non-zero on an unknown one."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--once", action="store_true", help="run a single poll cycle and exit"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="run a single cycle that reads the whole backfill window, then exit",
    )
    return parser.parse_args(argv)


def run(config, store, fetch, apply_fn, render_metrics, on_poll_ok, argv=None):
    """Loads persisted state, starts the metrics server, and runs the poll loop.

    Runs a single cycle and returns when `argv` carries --once or --backfill; otherwise
    starts a background HTTP server for /metrics and /healthz and polls Loki forever at
    `config.poll_interval`. A poll cycle's own exception is caught and logged by
    `poll_forever` rather than allowed to kill the loop.

    The per-game arguments are the same callables the rest of this module takes: `fetch`
    and `apply_fn` as `run_cycle` documents them, `render_metrics(state, now) -> str` for
    /metrics, and `on_poll_ok(state) -> str` for the per-cycle log line.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    once = args.once
    backfill = args.backfill
    poll_state = PollState(store.load_state())
    cursor = initial_cursor(
        store.get_cursor(), backfill, time.time(), config.backfill_days
    )
    log(
        "%s starting (loki=%s once=%s backfill=%s players=%d)"
        % (
            config.service_name,
            config.loki_url,
            once,
            backfill,
            len(poll_state.value.players),
        )
    )
    if not (once or backfill):
        # Threading server so a slow /metrics render cannot block the /healthz probe and trip
        # autoheal.
        start_metrics_server(
            make_handler(poll_state, render_metrics, config.health_max_age),
            config.metrics_port,
        )
    poll_forever(
        poll_state,
        store,
        cursor,
        fetch,
        apply_fn,
        config.page_limit,
        once,
        backfill,
        config.poll_interval,
        on_poll_ok,
    )
