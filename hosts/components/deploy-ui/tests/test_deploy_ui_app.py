"""The app's routing over an injected runner: every read degrades, every write is guarded."""

import dataclasses
import io
import json
import pathlib
import subprocess

import pytest

import deploy_ui
from deploy_ui_lib import deploy_ui_reads as reads
from lib.tree_lock import TREE_LOCK

TREE_LOCK_NAME = pathlib.PurePath(TREE_LOCK).name


class FakeRun:
    """Answers each argv prefix with canned (stdout, rc); records what was run."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        for prefix, (out, rc) in self.table.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if rc == "raise":
                    raise subprocess.TimeoutExpired(argv, 1)
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


# A landing holding the tree lock, and a process that is not a landing queued on it.
PS = """\
 4321     1   125 /x/python3 scripts/deploy_tools/land.py --pr 1543 --since 72c1a41
 5000     1    30 /x/python3 /opt/homelab/other_holder.py
"""
# fuser with stderr merged: `<path>: <pids>` for each OPEN file. The queued process (5000)
# has the tree lock open too. The argv names the lock path, so the prefix is the command
# alone; the path is filled in by the fixture, which knows the tmp dir.
FUSER = """\
{d}/{name}: 4321 5000
"""
TABLE: dict[tuple[str, ...], tuple[str, int | str]] = {
    ("ps", "-eo", "pid=,ppid=,etimes=,args="): (PS, 0),
    ("fuser",): (FUSER, 0),
    ("gh", "pr", "list"): (
        '[{"number":1550,"title":"T","headRefName":"b","isDraft":false,"statusCheckRollup":[]}]',
        0,
    ),
}
HDRS = {"X-Deploy-UI": "1", "Content-Type": "application/json"}


def _proc_locks_line(path, waiter=None):
    maj, mnr, ino = reads.lock_key(str(path))
    arrow, pid = ("-> ", waiter) if waiter else ("", 4242)
    return f"9: {arrow}FLOCK  ADVISORY  WRITE {pid} {maj:02x}:{mnr:02x}:{ino} 0 EOF\n"


@pytest.fixture
def lock_dir(tmp_path):
    """The tree lock, and a /proc/locks in which it is granted and 5000 is blocked on it.
    The key is the file's real (dev, inode)."""
    d = tmp_path / "lock"
    d.mkdir()
    (d / TREE_LOCK_NAME).touch()
    (tmp_path / "proc_locks").write_text(
        _proc_locks_line(d / TREE_LOCK_NAME)
        + _proc_locks_line(d / TREE_LOCK_NAME, waiter=5000)
    )
    return d


def _table(lock_dir):
    t = dict(TABLE)
    t[("fuser",)] = (FUSER.format(d=lock_dir, name=TREE_LOCK_NAME), 0)
    return t


def _bare_config(tmp_path):
    """A Config with no lock files and the default /proc/locks."""
    return deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path,
        bind="",
        port=0,
        tree_lock=tmp_path / TREE_LOCK_NAME,
    )


@pytest.fixture
def app(tmp_path, lock_dir):
    cfg = deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path / "logs",
        bind="127.0.0.1",
        port=0,
        tree_lock=lock_dir / TREE_LOCK_NAME,
        proc_locks=tmp_path / "proc_locks",
    )
    return deploy_ui.App(cfg, run=FakeRun(_table(lock_dir)))


def body(resp):
    return json.loads(resp[1])


def test_index_serves_the_page(app):
    status, html = app.get("/")
    assert status == 200 and "<title>Deploy queue</title>" in html


def test_page_offers_land_and_no_deploy_control_is_clean(app):
    _, html = app.get("/")
    assert "/api/land" in html and "/api/cancel" in html
    assert "/api/deploy" not in html and ">deploy<" not in html


def test_page_links_pr_numbers_to_github_is_clean(app):
    _, html = app.get("/")
    assert "const REPO='https://github.com/DanielH2018/server'" in html
    assert "r.pr?prLink(r.pr)" in html and "<td>${prLink(p.number)}</td>" in html


def test_inflight_landing_carries_its_pr_title_once_fetched_is_clean(app):
    app.run.table[("gh", "pr", "view", "1543")] = ("Fix the thing\n", 0)
    app.get("/api/inflight")
    rows = {r["pid"]: r for r in body(app.get("/api/inflight"))["runs"]}
    assert rows[4321]["title"] == "Fix the thing" and rows[5000]["title"] == ""
    assert sum(1 for c in app.run.calls if c[:3] == ["gh", "pr", "view"]) == 1


def test_inflight_title_lookup_failure_leaves_panel_up_is_flagged(app):
    app.run.table[("gh", "pr", "view")] = ("", 1)
    b = body(app.get("/api/inflight"))
    assert "unavailable" not in b
    assert {r["pid"]: r for r in b["runs"]}[4321]["title"] == ""
    app.get("/api/inflight")
    assert sum(1 for c in app.run.calls if c[:3] == ["gh", "pr", "view"]) == 2


def test_page_switches_logs_and_folds_the_finished_list_is_clean(app):
    _, html = app.get("/")
    assert 'id="vtabs"' in html and "function renderTabs()" in html
    assert "const FIN_SHOWN=5" in html and 'id="finToggle"' in html
    assert 'onclick="showStage(${d.stages.indexOf(s)})"' in html


def test_page_has_no_stale_release_panel_is_flagged(app):
    _, html = app.get("/")
    assert "/api/stale" not in html and 'id="stale"' not in html


def test_deploy_route_is_gone_and_spawns_nothing_is_flagged(app, monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(
        deploy_ui.writes,
        "spawn_logged",
        lambda argv, cwd, log_dir, action: spawned.append(argv) or tmp_path / "d.log",
    )
    monkeypatch.setattr(deploy_ui.writes, "audit", lambda line: None)
    status, _ = app.post("/api/deploy", HDRS, json.dumps({"tag": "n8n"}))
    assert status == 404 and spawned == []


def test_stale_route_is_gone_and_runs_nothing_is_flagged(app):
    status, _ = app.get("/api/stale")
    assert status == 404 and not any(c[0] == "uv" for c in app.run.calls)


def test_inflight_lists_the_tree_lock_holder_and_waiter_is_clean(app):
    b = body(app.get("/api/inflight"))
    rows = {r["pid"]: r for r in b["runs"]}
    assert set(rows) == {4321, 5000}
    assert rows[4321]["kind"] == "land" and rows[4321]["pr"] == "1543"
    assert rows[4321]["locks"] == [TREE_LOCK_NAME]
    # 5000 has the tree lock open like the holder does; /proc/locks says it is blocked on it.
    assert rows[5000]["kind"] == "lock"
    assert rows[5000]["locks"] == []
    assert rows[5000]["waiting_on"] == [TREE_LOCK_NAME]
    assert b["locks_watched"] == [TREE_LOCK_NAME]


def test_inflight_asks_fuser_about_the_tree_lock_is_clean(app, lock_dir):
    app.get("/api/inflight")
    fuser = next(c for c in app.run.calls if c[0] == "fuser")
    assert fuser == ["fuser", str(lock_dir / TREE_LOCK_NAME)]


def test_inflight_ignores_other_lock_files_in_the_tree_locks_directory_is_flagged(
    tmp_path, lock_dir, monkeypatch
):
    """A stale unit may still export DEPLOY_UI_LOCKS; nothing reads it."""
    (lock_dir / "server-deploy-x.lock").touch()
    monkeypatch.setenv("DEPLOY_UI_TREE_LOCK", str(lock_dir / TREE_LOCK_NAME))
    monkeypatch.setenv("DEPLOY_UI_LOCKS", str(lock_dir))
    cfg = dataclasses.replace(
        deploy_ui.Config.from_env(), proc_locks=tmp_path / "proc_locks"
    )
    run = FakeRun(_table(lock_dir))
    b = body(deploy_ui.App(cfg, run=run).get("/api/inflight"))
    assert b["locks_watched"] == [TREE_LOCK_NAME]
    fuser = next(c for c in run.calls if c[0] == "fuser")
    assert fuser == ["fuser", str(lock_dir / TREE_LOCK_NAME)]


def test_inflight_with_no_lock_files_says_so_is_flagged(tmp_path):
    """`free` over no files is the empty-panel trap; the page needs the count to tell."""
    empty = tmp_path / "nolocks"
    empty.mkdir()
    (tmp_path / "proc_locks").write_text("")
    cfg = deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path,
        bind="",
        port=0,
        tree_lock=empty / TREE_LOCK_NAME,
        proc_locks=tmp_path / "proc_locks",
    )
    run = FakeRun(dict(TABLE))
    b = body(deploy_ui.App(cfg, run=run).get("/api/inflight"))
    assert b["locks_watched"] == []
    assert not any(c[0] == "fuser" for c in run.calls)


def test_inflight_unreadable_proc_locks_is_unavailable_is_flagged(tmp_path, lock_dir):
    """A lock read that errors must never render `free`."""
    cfg = deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path,
        bind="",
        port=0,
        tree_lock=lock_dir / TREE_LOCK_NAME,
        proc_locks=tmp_path / "missing",
    )
    b = body(deploy_ui.App(cfg, run=FakeRun(_table(lock_dir))).get("/api/inflight"))
    assert "missing" in b["unavailable"]


def test_inflight_degrades_when_ps_fails_is_flagged(tmp_path):
    t = dict(TABLE)
    t[("ps", "-eo", "pid=,ppid=,etimes=,args=")] = ("", "raise")
    cfg = _bare_config(tmp_path)
    b = body(deploy_ui.App(cfg, run=FakeRun(t)).get("/api/inflight"))
    assert b["unavailable"].startswith("ps")


def test_prs_are_cached_is_clean(app):
    app.get("/api/prs")
    app.get("/api/prs")
    assert sum(1 for c in app.run.calls if c[:2] == ["gh", "pr"]) == 1


def test_prs_first_call_after_boot_runs_gh_is_clean(tmp_path, monkeypatch):
    """A fresh App's cache sentinel must not read as fresher than an early-boot clock."""
    cfg = _bare_config(tmp_path)
    run = FakeRun(dict(TABLE))
    app = deploy_ui.App(cfg, run=run)
    monkeypatch.setattr(deploy_ui.time, "monotonic", lambda: 5.0)
    body(app.get("/api/prs"))
    assert sum(1 for c in run.calls if c[:2] == ["gh", "pr"]) == 1


def test_post_without_header_is_flagged(app):
    status, _ = app.post(
        "/api/land",
        {"Content-Type": "application/json"},
        json.dumps({"pr": "1", "since": "a"}),
    )
    assert status == 403


def test_post_non_dict_body_is_flagged(app):
    status, _ = app.post("/api/land", HDRS, json.dumps([1, 2, 3]))
    assert status == 400


def test_land_spawns_land_sh_is_clean(app, monkeypatch, tmp_path):
    spawned = {}
    monkeypatch.setattr(
        deploy_ui.writes,
        "spawn_logged",
        lambda argv, cwd, log_dir, action: (
            spawned.setdefault("argv", argv) and tmp_path / "l.log"
        ),
    )
    monkeypatch.setattr(deploy_ui.writes, "audit", lambda line: None)
    status, _ = app.post(
        "/api/land", HDRS, json.dumps({"pr": "1550", "since": "72c1a41"})
    )
    assert status == 202
    assert spawned["argv"] == [
        "./scripts/deploy_tools/land.sh",
        "--pr",
        "1550",
        "--since",
        "72c1a41",
    ]


def test_land_duplicate_is_refused_is_flagged(app):
    status, text = app.post(
        "/api/land", HDRS, json.dumps({"pr": "1543", "since": "72c1a41"})
    )
    assert status == 409 and "1543" in text


def test_cancel_lock_row_is_flagged(app):
    """A lock row is listed in flight but is not cancellable: only a landing is."""
    status, text = app.post("/api/cancel", HDRS, json.dumps({"pid": 5000}))
    assert status == 409 and "5000" in text


def test_cancel_unlisted_pid_is_flagged(app):
    status, _ = app.post("/api/cancel", HDRS, json.dumps({"pid": 99}))
    assert status == 409


def test_cancel_non_numeric_pid_is_flagged(app):
    status, _ = app.post("/api/cancel", HDRS, json.dumps({"pid": "72c1a41"}))
    assert status == 400


def test_land_non_hex_since_is_flagged(app):
    status, text = app.post(
        "/api/land", HDRS, json.dumps({"pr": "1550", "since": "HEAD~1"})
    )
    assert status == 400 and "hex" in text


def test_read_raising_an_unexpected_error_is_unavailable_is_flagged(tmp_path):
    """A gh payload missing a field must reach the page as red text, not a traceback."""
    t = dict(TABLE)
    t[("gh", "pr", "list")] = ('[{"title":"no number here"}]', 0)
    cfg = _bare_config(tmp_path)
    b = body(deploy_ui.App(cfg, run=FakeRun(t)).get("/api/prs"))
    assert "prs" not in b
    assert b["unavailable"].startswith("KeyError")


def test_content_length_reads_a_byte_count_is_clean():
    assert deploy_ui.content_length({"Content-Length": "17"}) == 17
    assert deploy_ui.content_length({}) == 0


def test_content_length_non_numeric_is_flagged():
    assert deploy_ui.content_length({"Content-Length": "seventeen"}) is None
    assert deploy_ui.content_length({"Content-Length": "-1"}) is None


def test_request_line_names_a_page_load_and_its_user_is_clean():
    assert (
        deploy_ui.request_line("GET", "/?x=1", 200, "daniel")
        == "request method=GET path=/ status=200 user=daniel"
    )


def test_request_line_drops_the_query_and_neuters_a_hostile_user_is_flagged():
    """A log path in the query and a header carrying spaces must not reach journald raw:
    either would split the logfmt line the usage count parses."""
    line = deploy_ui.request_line(
        "GET", "/api/log?path=/x/y z", 200, 'a b="c"\nrequest method=POST'
    )
    assert line.startswith("request method=GET path=/api/log status=200 user=a_b_")
    assert line.count("=") == 4
    assert deploy_ui.request_line("POST", "/api/land", 409, "").endswith("user=-")


def test_request_line_quotes_a_path_logfmt_would_split_is_flagged():
    """The line is host_lib's logfmt (#6202): a path with a space stays one `path` value."""
    assert deploy_ui.request_line("GET", "/a b", 404, "u") == (
        'request method=GET path="/a b" status=404 user=u'
    )


class _ClosedSocket(io.BufferedIOBase):
    def __init__(self, error):
        self.error = error

    def write(self, data):
        raise self.error


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError])
def test_client_closing_mid_response_logs_one_line_no_traceback_is_flagged(
    app, capsys, error
):
    """A disconnect raised out of the handler, and socketserver printed its traceback,
    which HostLogErrors counts (#7921)."""
    handler = object.__new__(deploy_ui.make_handler(app))
    handler.rfile = io.BytesIO(b"GET / HTTP/1.1\r\nRemote-User: daniel\r\n\r\n")
    handler.wfile = _ClosedSocket(error())
    handler.client_address = ("10.42.0.113", 40000)
    handler.close_connection = False
    handler.handle_one_request()
    assert handler.close_connection
    assert capsys.readouterr().err.splitlines() == [
        "request method=GET path=/ status=200 user=daniel",
        "client-disconnected method=GET path=/ status=200 user=daniel",
    ]


def _games_app(tmp_path, table, loki_url="https://loki.example"):
    cfg = dataclasses.replace(
        _bare_config(tmp_path),
        games=deploy_ui.game_reads.parse_games("valheim=valheim,valheim-stats"),
        loki_url=loki_url,
    )
    return deploy_ui.App(cfg, run=FakeRun(table))


def test_games_reads_the_deployments_and_their_pods_is_clean(tmp_path):
    deployments = {
        "items": [
            {"metadata": {"name": n}, "spec": {"replicas": 0}, "status": {}}
            for n in ("valheim", "valheim-stats")
        ]
    }
    app = _games_app(
        tmp_path,
        {
            ("kubectl", "-n", "homelab", "get", "deployments"): (
                json.dumps(deployments),
                0,
            ),
            ("kubectl", "-n", "homelab", "get", "pods"): ('{"items": []}', 0),
        },
    )
    [game] = body(app.get("/api/games"))["games"]
    assert (game["name"], game["state"]) == ("valheim", "stopped")
    pods_call = next(c for c in app.run.calls if "pods" in c)
    assert pods_call[pods_call.index("-l") + 1] == "app in (valheim,valheim-stats)"


def test_games_when_kubectl_fails_is_unavailable_is_flagged(tmp_path):
    app = _games_app(tmp_path, {("kubectl",): ("", 1)})
    assert "kubectl exited 1" in body(app.get("/api/games"))["unavailable"]


def test_game_console_queries_loki_for_the_server_container_is_clean(tmp_path):
    loki = {"data": {"result": [{"values": [["2000000000", "Game server connected"]]}]}}
    app = _games_app(tmp_path, {("curl",): (json.dumps(loki), 0)})
    got = body(app.get("/api/game-console?game=valheim"))
    assert got == {
        "game": "valheim",
        "lines": [{"ts": 2, "line": "Game server connected"}],
    }
    [call] = app.run.calls
    assert "https://loki.example/loki/api/v1/query_range" in call
    assert 'query={namespace="homelab",container="valheim"}' in call


@pytest.mark.parametrize(
    ("path", "loki_url", "reason"),
    [
        ("/api/game-console?game=minecraft", "https://loki.example", "no game named"),
        ("/api/game-console?game=valheim", "", "DEPLOY_UI_LOKI is not set"),
    ],
)
def test_game_console_refusals_are_unavailable_is_flagged(
    tmp_path, path, loki_url, reason
):
    app = _games_app(tmp_path, {}, loki_url=loki_url)
    assert reason in body(app.get(path))["unavailable"]
