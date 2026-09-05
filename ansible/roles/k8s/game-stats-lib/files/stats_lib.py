#!/usr/bin/env python3
"""stats_lib — shared Loki-tail -> Prometheus-exporter skeleton for the game-stats roles.

Shipped as a sibling copy beside valheim_stats.py and stats.py (terraria-stats), the same
mechanism `host_lib.py` uses from `roles/setup/common`: a directly-invoked script gets only
its own directory on `sys.path`, so a shared module has to be copied in rather than imported
across the tree (see the repo-root CLAUDE.md). This one ships into a ConfigMap and runs
inside a python:3.14-alpine pod rather than on a host, so it is staged by
`roles/k8s/game-stats-lib/tasks/stage.yml`, not `install_host_lib.yml` — see that file's
header for why a new role owns it instead of extending host_lib.py or having one game role
own it for the other.

Both stats roles tail a game's console out of Loki, parse join/leave-shaped lines, fold them
into all-time per-player stats kept in SQLite, and serve Prometheus metrics. They were built
as two independent forks and diverged only in the ~250 lines that genuinely differ per game:
the line grammar (Valheim's console differs from Terraria's, not as a dialect but as a
different language — see valheim_stats.py's own docstring), the state machine each grammar
needs (Valheim tracks deaths and a SteamID<->name map; Terraria does not), and the SQLite
schema that persists that state. Those three stay in each role's own file.

What lives here instead — the mechanical skeleton neither game's identity touches:
  - the env reader (`env`)
  - Loki fetch (`http_get_json`, `build_query_range_url`, `extract_entries`)
  - cursor handling (`initial_cursor`, `run_cycle`)
  - metric rendering/escaping (`escape_label_value`, `render_family`)
  - the HTTP handler (`PollState`, `make_handler`, `start_metrics_server`)
  - the run loop (`poll_forever`)
  - a timestamped logger (`log`)

Every function here is stdlib-only and takes its game-specific bits (URLs, the per-game
`apply_fn`, the per-game `render_metrics`/`on_poll_ok` callables) as arguments rather than
reading a module-level constant, so this module carries no per-game state of its own and
needs no test doubles patched onto it — a caller passes a fake `fetch`/`apply_fn` instead.
"""

import json
import os
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


def extract_entries(loki_json):
    """Flatten a Loki query_range response to [(ts_ns:int, line:str)] ascending."""
    out = []
    for stream in loki_json.get("data", {}).get("result", []):
        for ts, line in stream.get("values", []):
            out.append((int(ts), line))
    out.sort(key=lambda tl: tl[0])
    return out


# cursor handling
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

    `fetch(start_ns, end_ns) -> [(ts_ns, line)]`. `apply_fn(state, entries) -> (events,
    max_ts)` is the per-game fold (e.g. Valheim's excludes heartbeats from the audit log,
    Terraria's has no heartbeat at all). Pages until a short/empty page. State mutation +
    cursor advance are persisted together so a crash re-runs the batch cleanly (events past
    the saved cursor simply re-apply on next start).
    """
    while True:
        entries = fetch(cursor, end_ns)
        if not entries:
            break
        events, max_ts = apply_fn(state, entries)
        if max_ts > cursor:
            cursor = max_ts
        store.save(state, cursor, events)
        if len(entries) < page_limit:
            break
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


def make_handler(poll_state, render_metrics_fn, health_max_age):
    """Builds a BaseHTTPRequestHandler serving /metrics and /healthz.

    `render_metrics_fn(state, now) -> str` is the per-game exposition renderer.
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
                    body = render_metrics_fn(poll_state.value, time.time()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/healthz":
                fresh = (time.time() - poll_state.last_poll_ok) < health_max_age
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
    cannot kill the loop. `on_poll_ok(state) -> str` describes a successful cycle for the
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
        if once or backfill:
            return
        time.sleep(poll_interval)
