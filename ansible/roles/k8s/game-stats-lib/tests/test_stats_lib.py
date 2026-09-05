"""Behavioural tests for stats_lib — the shared skeleton valheim_stats.py and stats.py stage
beside themselves (see the module's own docstring and roles/k8s/game-stats-lib/tasks/stage.yml
for the shipping mechanism). Each per-game role's own tests still cover its parser, state
machine and SQLite schema; this file covers the parts that moved here.
"""

import json
import threading
import time
from unittest import mock

import stats_lib


def test_env_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("SOME_STATS_LIB_VAR", "42")
    assert stats_lib.env("SOME_STATS_LIB_VAR", "0") == "42"


def test_env_falls_back_to_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_STATS_LIB_VAR", raising=False)
    assert stats_lib.env("SOME_UNSET_STATS_LIB_VAR", "fallback") == "fallback"


def test_log_prints_a_timestamped_line(capsys):
    stats_lib.log("hello", "world")
    out = capsys.readouterr().out
    assert out.startswith("[")
    assert "hello world" in out


def test_extract_entries_sorts_ascending():
    payload = {
        "data": {
            "result": [
                {"values": [["20", "b"], ["10", "a"]]},
            ]
        }
    }
    assert stats_lib.extract_entries(payload) == [(10, "a"), (20, "b")]


def test_extract_entries_handles_an_empty_result():
    assert stats_lib.extract_entries({"data": {"result": []}}) == []


def test_build_query_range_url_carries_the_query_and_window():
    url = stats_lib.build_query_range_url(
        "http://loki:3100/", '{container="x"}', 100, 200, 5000
    )
    assert url.startswith("http://loki:3100/loki/api/v1/query_range?")
    assert "start=101" in url  # (start_ns, end_ns] — exclusive start, so start+1
    assert "end=200" in url
    assert "limit=5000" in url


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_http_get_json_sets_the_user_agent_and_parses_the_body():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _Resp({"ok": True})

    with mock.patch("stats_lib.urllib.request.urlopen", fake_urlopen):
        body = stats_lib.http_get_json("http://loki/x", 10, "valheim-stats")
    assert body == {"ok": True}
    assert captured["timeout"] == 10
    # urllib capitalises header keys ("User-agent"); assert on the value to stay robust.
    assert "valheim-stats" in captured["req"].headers.values()


def test_initial_cursor_bounds_a_fresh_db_to_the_backfill_window():
    now = 1_000_000.0
    days = 28
    expected = int((now - days * 86400) * 1e9)
    assert stats_lib.initial_cursor(0, False, now, days) == expected


def test_initial_cursor_bounds_an_explicit_backfill_regardless_of_stored_cursor():
    now = 1_000_000.0
    days = 28
    expected = int((now - days * 86400) * 1e9)
    assert stats_lib.initial_cursor(5_000, True, now, days) == expected


def test_initial_cursor_resumes_from_a_stored_cursor():
    assert stats_lib.initial_cursor(12_345, False, 1_000_000.0, 28) == 12_345


def test_escape_label_value_escapes_backslash_quote_and_newline():
    assert stats_lib.escape_label_value('He said "hi"\\n') == 'He said \\"hi\\"\\\\n'


class _FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, state, cursor_ns, events=()):
        self.saved.append((cursor_ns, list(events)))


def test_run_cycle_pages_until_a_short_page_and_persists_each_page():
    pages = [
        [(1_000, "a")] * 3,  # a full page (page_limit=3) triggers another fetch
        [(4_000, "b")],  # a short page ends paging
    ]

    def fake_fetch(start_ns, end_ns):
        return pages.pop(0) if pages else []

    def fake_apply(state, entries):
        # max_ts is the last entry's timestamp; events pass through unchanged.
        return list(entries), entries[-1][0]

    store = _FakeStore()
    cursor = stats_lib.run_cycle(
        state=object(),
        store=store,
        cursor=0,
        end_ns=9_000,
        fetch=fake_fetch,
        apply_fn=fake_apply,
        page_limit=3,
    )
    assert cursor == 4_000
    assert [c for c, _ in store.saved] == [1_000, 4_000]


def test_run_cycle_returns_the_cursor_unchanged_on_no_new_entries():
    cursor = stats_lib.run_cycle(
        state=object(),
        store=_FakeStore(),
        cursor=555,
        end_ns=9_000,
        fetch=lambda s, e: [],
        apply_fn=lambda state, entries: (list(entries), 0),
        page_limit=5000,
    )
    assert cursor == 555


def test_render_family_scalar_gauge():
    lines = []
    stats_lib.render_family(lines, "x_online", "how many.", "gauge", [3])
    assert "# HELP x_online how many." in lines
    assert "# TYPE x_online gauge" in lines
    assert "x_online 3" in lines


def test_render_family_labelled_counter_escapes_and_sorts_as_given():
    lines = []
    stats_lib.render_family(
        lines,
        "x_playtime_seconds_total",
        "seconds played.",
        "counter",
        [("Bob", 10), ('He said "hi"', 5)],
        label_name="player",
    )
    assert 'x_playtime_seconds_total{player="Bob"} 10' in lines
    assert 'x_playtime_seconds_total{player="He said \\"hi\\""} 5' in lines


def test_poll_state_starts_with_the_given_value_and_zero_last_poll_ok():
    state = object()
    ps = stats_lib.PollState(state)
    assert ps.value is state
    assert ps.last_poll_ok == 0.0
    assert isinstance(ps.lock, type(threading.Lock()))


def test_poll_forever_stamps_last_poll_ok_and_stops_on_once():
    ps = stats_lib.PollState("state")
    described = []

    def fake_run_cycle(state, store, cursor, end_ns, fetch, apply_fn, page_limit):
        return cursor + 1

    with mock.patch("stats_lib.run_cycle", fake_run_cycle):
        stats_lib.poll_forever(
            ps,
            store=None,
            cursor=0,
            fetch=None,
            apply_fn=None,
            page_limit=5000,
            once=True,
            backfill=False,
            poll_interval=20,
            on_poll_ok=lambda state: described.append(state) or "described",
        )
    assert ps.last_poll_ok > 0.0
    assert described == ["state"]


def test_poll_forever_logs_and_continues_on_a_cycle_exception():
    ps = stats_lib.PollState("state")
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("loki unreachable")

    logged = []
    with (
        mock.patch("stats_lib.run_cycle", boom),
        mock.patch("stats_lib.log", lambda *args: logged.append(args)),
    ):
        stats_lib.poll_forever(
            ps,
            store=None,
            cursor=0,
            fetch=None,
            apply_fn=None,
            page_limit=5000,
            once=True,
            backfill=False,
            poll_interval=20,
            on_poll_ok=lambda state: "unused",
        )
    assert calls["n"] == 1
    assert ps.last_poll_ok == 0.0  # never stamped — the cycle raised before it could
    assert any("poll error:" in args for args in logged)


def test_make_handler_serves_metrics_and_healthz_over_a_real_socket():
    """One end-to-end pass through the real HTTP handler, rather than unit-testing do_GET's
    branches in isolation — the routing, the lock, and the health-staleness math all meet
    here, and this is the shape both stats roles actually run in production."""
    import http.client

    ps = stats_lib.PollState("the-state")
    ps.last_poll_ok = time.time()

    def render(state, now):
        return "metric_x 1\n" if state == "the-state" else "wrong-state\n"

    handler_cls = stats_lib.make_handler(ps, render, health_max_age=30)
    server = __import__(
        "http.server", fromlist=["ThreadingHTTPServer"]
    ).ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.read() == b"metric_x 1\n"

        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()

        conn.request("GET", "/nope")
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_make_handler_healthz_is_stale_past_health_max_age():
    import http.client

    ps = stats_lib.PollState("s")
    ps.last_poll_ok = time.time() - 1000  # long past health_max_age
    handler_cls = stats_lib.make_handler(
        ps, lambda state, now: "x 1\n", health_max_age=30
    )
    server = __import__(
        "http.server", fromlist=["ThreadingHTTPServer"]
    ).ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 503
        resp.read()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
