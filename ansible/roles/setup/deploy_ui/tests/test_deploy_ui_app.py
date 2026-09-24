"""The app's routing over an injected runner: every read degrades, every write is guarded."""

import json
import pathlib
import subprocess
import urllib.parse

import pytest

import deploy_ui
import deploy_ui_reads as reads


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


# A landing, a page-spawned deploy (deploy.sh and the playbook it runs, holding its two
# service locks), and a second deploy of the same service queued on that lock.
PS = """\
 4321     1   125 /x/python3 scripts/deploy_tools/land.py --pr 1543 --since 72c1a41
 5000     1    90 bash ./scripts/deploy.sh --tags n8n
 5001  5000    88 /x/python3 /x/ansible-playbook ansible/deploy.yml --tags n8n
 5100     1    30 bash ./scripts/deploy.sh --tags n8n
"""
# fuser with stderr merged: `<path>: <pids>` for each OPEN file. The queued deploy
# (5100) has the n8n lock open too: deploy.sh opens the descriptor, then blocks on it.
# The argv names every lock file under the App's lock_dir, so the prefix is the command
# alone; the paths are filled in by the fixture, which knows the tmp dir.
FUSER = """\
{d}/server-deploy-all.lock: 5000 5001
{d}/server-deploy-n8n.lock: 5000 5001 5100
"""
TABLE = {
    ("ps", "-eo", "pid=,ppid=,etimes=,args="): (PS, 0),
    ("fuser",): (FUSER, 0),
    (
        "uv",
        "run",
        "python",
        "scripts/diagnostics/probe.py",
        "releases",
        "--stale-only",
    ): ("n8n: changed\n", 1),
    ("gh", "pr", "list"): (
        '[{"number":1550,"title":"T","headRefName":"b","isDraft":false,"statusCheckRollup":[]}]',
        0,
    ),
    # Shaped like the real output: `--list-services` prints the block tags and `always`
    # alongside the service names, which is the whole reason `_deployable_tags` subtracts.
    ("./scripts/deploy.sh", "--list-services"): (
        "always\nconfig\ncron\ndeploy\nhomepage\nn8n\n",
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
    """Three lock files, and a /proc/locks in which all.lock and n8n.lock are granted
    and 5100 is blocked on n8n.lock. Keys are the files' real (dev, inode)."""
    d = tmp_path / "lock"
    d.mkdir()
    for name in (
        "server-git-tree.lock",
        "server-deploy-all.lock",
        "server-deploy-n8n.lock",
    ):
        (d / name).touch()
    (tmp_path / "proc_locks").write_text(
        _proc_locks_line(d / "server-deploy-all.lock")
        + _proc_locks_line(d / "server-deploy-n8n.lock")
        + _proc_locks_line(d / "server-deploy-n8n.lock", waiter=5100)
    )
    return d


def _table(lock_dir):
    t = dict(TABLE)
    t[("fuser",)] = (FUSER.format(d=lock_dir), 0)
    return t


@pytest.fixture
def app(tmp_path, state_dir, lock_dir):
    cfg = deploy_ui.Config(
        repo=tmp_path,
        state_dir=state_dir,
        log_dir=tmp_path / "logs",
        bind="127.0.0.1",
        port=0,
        lock_dir=lock_dir,
        proc_locks=tmp_path / "proc_locks",
    )
    return deploy_ui.App(cfg, run=FakeRun(_table(lock_dir)))


def body(resp):
    return json.loads(resp[1])


def test_index_serves_the_page(app):
    status, html = app.get("/")
    assert status == 200 and "<title>Deploy queue</title>" in html


def test_inflight_lists_landing_deploy_and_queued_deploy_is_clean(app):
    b = body(app.get("/api/inflight"))
    rows = {r["pid"]: r for r in b["runs"]}
    assert rows[4321]["kind"] == "land" and rows[4321]["pr"] == "1543"
    # The playbook (5001) folds into the deploy.sh that spawned it; the row carries the
    # locks the family holds, and the tree lock is one of the files fuser was asked about.
    assert set(rows) == {4321, 5000, 5100}
    assert rows[5000]["kind"] == "deploy" and rows[5000]["tag"] == "n8n"
    assert rows[5000]["locks"] == ["server-deploy-all.lock", "server-deploy-n8n.lock"]
    # 5100 has n8n.lock open like the holder does; /proc/locks says it is blocked on it.
    assert rows[5100]["locks"] == []
    assert rows[5100]["waiting_on"] == ["server-deploy-n8n.lock"]
    assert b["locks_watched"] == [
        "server-git-tree.lock",
        "server-deploy-all.lock",
        "server-deploy-n8n.lock",
    ]


def test_inflight_asks_fuser_about_every_lock_file_is_clean(app, lock_dir):
    app.get("/api/inflight")
    fuser = next(c for c in app.run.calls if c[0] == "fuser")
    assert fuser == [
        "fuser",
        *(
            str(lock_dir / n)
            for n in (
                "server-git-tree.lock",
                "server-deploy-all.lock",
                "server-deploy-n8n.lock",
            )
        ),
    ]


def test_inflight_with_no_lock_files_says_so_is_flagged(tmp_path, state_dir):
    """`free` over no files is the empty-panel trap; the page needs the count to tell."""
    empty = tmp_path / "nolocks"
    empty.mkdir()
    (tmp_path / "proc_locks").write_text("")
    cfg = deploy_ui.Config(
        repo=tmp_path,
        state_dir=state_dir,
        log_dir=tmp_path,
        bind="",
        port=0,
        lock_dir=empty,
        proc_locks=tmp_path / "proc_locks",
    )
    app = deploy_ui.App(cfg, run=FakeRun(dict(TABLE)))
    b = body(app.get("/api/inflight"))
    assert b["locks_watched"] == []
    assert not any(c[0] == "fuser" for c in app.run.calls)


def test_inflight_unreadable_proc_locks_is_unavailable_is_flagged(
    tmp_path, state_dir, lock_dir
):
    """A lock read that errors must never render `free`."""
    cfg = deploy_ui.Config(
        repo=tmp_path,
        state_dir=state_dir,
        log_dir=tmp_path,
        bind="",
        port=0,
        lock_dir=lock_dir,
        proc_locks=tmp_path / "missing",
    )
    b = body(deploy_ui.App(cfg, run=FakeRun(_table(lock_dir))).get("/api/inflight"))
    assert "missing" in b["unavailable"]


def test_inflight_degrades_when_ps_fails_is_flagged(tmp_path, state_dir):
    t = dict(TABLE)
    t[("ps", "-eo", "pid=,ppid=,etimes=,args=")] = ("", "raise")
    cfg = deploy_ui.Config(
        repo=tmp_path, state_dir=state_dir, log_dir=tmp_path, bind="", port=0
    )
    b = body(deploy_ui.App(cfg, run=FakeRun(t)).get("/api/inflight"))
    assert b["unavailable"].startswith("ps")


def test_stale_rows_is_clean(app):
    assert body(app.get("/api/stale"))["stale"] == [
        {"service": "n8n", "reason": "changed"}
    ]


def test_stale_is_cached_is_clean(app):
    app.get("/api/stale")
    app.get("/api/stale")
    assert sum(1 for c in app.run.calls if c[:1] == ["uv"]) == 1


def test_state_reads_markers_is_clean(app, state_dir):
    (state_dir / "hold_sha").write_text("deadbeef")
    assert body(app.get("/api/state"))["hold_sha"] == "deadbeef"


def test_state_unreadable_marker_is_unavailable_and_land_refuses(app, state_dir):
    marker = state_dir / "hold_sha"
    marker.write_text("deadbeef")
    marker.chmod(0o000)
    try:
        assert "unavailable" in body(app.get("/api/state"))
        status, _ = app.post(
            "/api/land", HDRS, json.dumps({"pr": "9999", "since": "72c1a41"})
        )
        assert status == 503
    finally:
        marker.chmod(0o644)


def test_prs_are_cached_is_clean(app):
    app.get("/api/prs")
    app.get("/api/prs")
    assert sum(1 for c in app.run.calls if c[:2] == ["gh", "pr"]) == 1


def test_prs_first_call_after_boot_runs_gh_is_clean(tmp_path, state_dir, monkeypatch):
    """A fresh App's cache sentinel must not read as fresher than an early-boot clock."""
    cfg = deploy_ui.Config(
        repo=tmp_path,
        state_dir=state_dir,
        log_dir=tmp_path / "logs",
        bind="127.0.0.1",
        port=0,
    )
    app = deploy_ui.App(cfg, run=FakeRun(dict(TABLE)))
    monkeypatch.setattr(deploy_ui.time, "monotonic", lambda: 5.0)
    body(app.get("/api/prs"))
    assert sum(1 for c in app.run.calls if c[:2] == ["gh", "pr"]) == 1


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


def test_deployable_tags_drops_the_listed_block_tags(app):
    assert app._deployable_tags() == {"homepage", "n8n"}


def test_deploy_block_tag_is_flagged(app):
    """A tag `--list-services` prints must still not start a fleet-wide deploy (#1596).

    `config` selects the config half of every container role, so this arriving as a 202 is
    the defect: the page's only deploy button posts one service name from the stale panel.
    """
    status, text = app.post("/api/deploy", HDRS, json.dumps({"tag": "config"}))
    assert status == 409 and "config" in text


def test_deploy_unknown_tag_is_flagged(app):
    status, text = app.post("/api/deploy", HDRS, json.dumps({"tag": "nope"}))
    assert status == 409 and "nope" in text


def test_deploy_known_tag_spawns_deploy_sh_is_clean(app, monkeypatch, tmp_path):
    spawned = {}
    monkeypatch.setattr(
        deploy_ui.writes,
        "spawn_logged",
        lambda argv, cwd, log_dir, action: (
            spawned.setdefault("argv", argv) and tmp_path / "d.log"
        ),
    )
    monkeypatch.setattr(deploy_ui.writes, "audit", lambda line: None)
    status, _ = app.post("/api/deploy", HDRS, json.dumps({"tag": "n8n"}))
    assert status == 202
    assert spawned["argv"] == ["./scripts/deploy.sh", "--tags", "n8n"]


def test_hold_clear_pair_is_clean(app, state_dir, monkeypatch):
    """Both markers go, and the reply names every plane that went with them (#2453).

    A Clear drops each `hold_plane` entry whatever is still unapplied, so the reply is the
    last place that can name them: nothing records those planes afterwards.
    """
    monkeypatch.setattr(deploy_ui.writes, "audit", lambda line: None)
    (state_dir / "hold_sha").write_text("deadbeef")
    (state_dir / "hold_plane").write_text("k3s; ansible/deploy.yml sonarr")
    status, text = app.post(
        "/api/hold/clear", HDRS, json.dumps({"expected_sha": "deadbeef"})
    )
    assert status == 200 and not (state_dir / "hold_plane").exists()
    assert "k3s" in text and "ansible/deploy.yml sonarr" in text


def test_cancel_deploy_row_is_flagged(app):
    """A deploy is listed in flight but is not cancellable: SIGTERM mid-play is exit 20."""
    status, text = app.post("/api/cancel", HDRS, json.dumps({"pid": 5000}))
    assert status == 409 and "5000" in text


def test_cancel_unlisted_pid_is_flagged(app):
    status, _ = app.post("/api/cancel", HDRS, json.dumps({"pid": 99}))
    assert status == 409


def test_cancel_non_numeric_pid_is_flagged(app):
    status, _ = app.post("/api/cancel", HDRS, json.dumps({"pid": "72c1a41"}))
    assert status == 400


def test_log_tail_serves_path_under_log_dir_is_clean(app, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    f = log_dir / "l.log"
    f.write_text("hello log")
    status, text = app.get("/api/log?path=" + urllib.parse.quote(str(f)))
    assert status == 200 and text == "hello log"


def test_log_tail_refuses_path_outside_log_dir_is_flagged(app, tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_text("secret")
    status, text = app.get("/api/log?path=" + urllib.parse.quote(str(outside)))
    assert status == 200 and text == "refused: not a deploy-ui log"


def test_log_tail_refuses_symlink_escaping_log_dir_is_flagged(app, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    outside = tmp_path / "secret.log"
    outside.write_text("secret")
    link = log_dir / "l.log"
    link.symlink_to(outside)
    status, text = app.get("/api/log?path=" + urllib.parse.quote(str(link)))
    assert status == 200 and text == "refused: not a deploy-ui log"


def test_land_non_hex_since_is_flagged(app):
    status, text = app.post(
        "/api/land", HDRS, json.dumps({"pr": "1550", "since": "HEAD~1"})
    )
    assert status == 400 and "hex" in text


def test_stale_rc1_with_no_rows_is_unavailable_is_flagged(tmp_path, state_dir):
    """probe.py exits 1 when it crashes too, and an empty panel reads as 'nothing pending'."""
    t = dict(TABLE)
    t[
        (
            "uv",
            "run",
            "python",
            "scripts/diagnostics/probe.py",
            "releases",
            "--stale-only",
        )
    ] = ("", 1)
    cfg = deploy_ui.Config(
        repo=tmp_path, state_dir=state_dir, log_dir=tmp_path, bind="", port=0
    )
    b = body(deploy_ui.App(cfg, run=FakeRun(t)).get("/api/stale"))
    assert "stale" not in b
    assert b["unavailable"].startswith("probe.py releases exited 1")


def test_read_raising_an_unexpected_error_is_unavailable_is_flagged(
    tmp_path, state_dir
):
    """A gh payload missing a field must reach the page as red text, not a traceback."""
    t = dict(TABLE)
    t[("gh", "pr", "list")] = ('[{"title":"no number here"}]', 0)
    cfg = deploy_ui.Config(
        repo=tmp_path, state_dir=state_dir, log_dir=tmp_path, bind="", port=0
    )
    b = body(deploy_ui.App(cfg, run=FakeRun(t)).get("/api/prs"))
    assert "prs" not in b
    assert b["unavailable"].startswith("KeyError")


def test_content_length_reads_a_byte_count_is_clean():
    assert deploy_ui.content_length({"Content-Length": "17"}) == 17
    assert deploy_ui.content_length({}) == 0


def test_content_length_non_numeric_is_flagged():
    assert deploy_ui.content_length({"Content-Length": "seventeen"}) is None
    assert deploy_ui.content_length({"Content-Length": "-1"}) is None


DEPLOY_SH = pathlib.Path(__file__).resolve().parents[5] / "scripts/deploy.sh"


def test_lock_names_agree_with_deploy_locks_is_clean(monkeypatch):
    """The daemon runs outside the venv and cannot import the lock names, so this is
    the literal-agreement guard: the tree lock path and the service-lock shape the page
    watches are the ones `deploy_locks.py` names -- the one module that names them, for
    the deployer and, through `deploy_locks.py plan`, for deploy.sh (issue #2054)."""
    import deploy_locks

    monkeypatch.delenv("HOMELAB_DEPLOY_LOCK_DIR", raising=False)
    assert deploy_locks.TREE_LOCK == f"/var/lock/{deploy_ui.TREE_LOCK}"
    assert f'"${{HOMELAB_DEPLOY_TREE_LOCK:-{deploy_locks.TREE_LOCK}}}"' in (
        DEPLOY_SH.read_text()
    )
    lock_dir = deploy_ui.Config.__dataclass_fields__["lock_dir"].default
    assert str(lock_dir) == deploy_locks.lock_dir() == "/var/lock"
    for name in (deploy_locks.SERVICE_LOCK_ALL, "sonarr", "pi-peer-backup"):
        path = pathlib.PurePath(deploy_locks.lock_path(name))
        assert path.parent == lock_dir
        assert path.match(deploy_ui.SERVICE_LOCK_GLOB), (name, path)


def test_the_service_lock_glob_does_not_match_the_tree_lock_is_flagged():
    """The reject half: a glob loose enough to take the tree lock for a service lock would
    show the tick as a deploy of a service called `git-tree`."""
    import deploy_locks

    assert not pathlib.PurePath(deploy_locks.TREE_LOCK).match(
        deploy_ui.SERVICE_LOCK_GLOB
    )
