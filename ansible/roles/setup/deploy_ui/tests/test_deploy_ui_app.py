"""The app's routing over an injected runner: every read degrades, every write is guarded."""

import json
import subprocess

import pytest

import deploy_ui


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


PS = " 4321   125 /x/python3 scripts/deploy_tools/land.py --pr 1543 --since abc\n"
TABLE = {
    ("ps",): (PS, 0),
    ("fuser",): ("4321", 0),
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
    ("./scripts/deploy.sh", "--list-services"): ("n8n\nhomepage\n", 0),
}
HDRS = {"X-Deploy-UI": "1", "Content-Type": "application/json"}


@pytest.fixture
def app(tmp_path, state_dir):
    cfg = deploy_ui.Config(
        repo=tmp_path,
        state_dir=state_dir,
        log_dir=tmp_path / "logs",
        bind="127.0.0.1",
        port=0,
    )
    return deploy_ui.App(cfg, run=FakeRun(dict(TABLE)))


def body(resp):
    return json.loads(resp[1])


def test_index_serves_the_page(app):
    status, html = app.get("/")
    assert status == 200 and "<title>Deploy queue</title>" in html


def test_inflight_lists_landings_and_holder_is_clean(app):
    b = body(app.get("/api/inflight"))
    assert b["landings"][0]["pr"] == "1543"
    assert b["lock_holder"]["pid"] == 4321


def test_inflight_degrades_when_ps_fails_is_flagged(tmp_path, state_dir):
    t = dict(TABLE)
    t[("ps",)] = ("", "raise")
    cfg = deploy_ui.Config(
        repo=tmp_path, state_dir=state_dir, log_dir=tmp_path, bind="", port=0
    )
    b = body(deploy_ui.App(cfg, run=FakeRun(t)).get("/api/inflight"))
    assert b["unavailable"].startswith("ps")


def test_stale_rows_is_clean(app):
    assert body(app.get("/api/stale"))["stale"] == [
        {"service": "n8n", "reason": "changed"}
    ]


def test_state_reads_markers_is_clean(app, state_dir):
    (state_dir / "hold_sha").write_text("deadbeef")
    assert body(app.get("/api/state"))["hold_sha"] == "deadbeef"


def test_prs_are_cached_is_clean(app):
    app.get("/api/prs")
    app.get("/api/prs")
    assert sum(1 for c in app.run.calls if c[:2] == ["gh", "pr"]) == 1


def test_post_without_header_is_flagged(app):
    status, _ = app.post(
        "/api/land",
        {"Content-Type": "application/json"},
        json.dumps({"pr": "1", "since": "a"}),
    )
    assert status == 403


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
    status, _ = app.post("/api/land", HDRS, json.dumps({"pr": "1550", "since": "abc"}))
    assert status == 202
    assert spawned["argv"] == [
        "./scripts/deploy_tools/land.sh",
        "--pr",
        "1550",
        "--since",
        "abc",
    ]


def test_land_duplicate_is_refused_is_flagged(app):
    status, text = app.post(
        "/api/land", HDRS, json.dumps({"pr": "1543", "since": "abc"})
    )
    assert status == 409 and "1543" in text


def test_deploy_unknown_tag_is_flagged(app):
    status, text = app.post("/api/deploy", HDRS, json.dumps({"tag": "nope"}))
    assert status == 409 and "nope" in text


def test_hold_clear_pair_is_clean(app, state_dir, monkeypatch):
    monkeypatch.setattr(deploy_ui.writes, "audit", lambda line: None)
    (state_dir / "hold_sha").write_text("deadbeef")
    (state_dir / "hold_plane").write_text("k3s")
    status, _ = app.post(
        "/api/hold/clear", HDRS, json.dumps({"expected_sha": "deadbeef"})
    )
    assert status == 200 and not (state_dir / "hold_plane").exists()


def test_cancel_unlisted_pid_is_flagged(app):
    status, _ = app.post("/api/cancel", HDRS, json.dumps({"pid": 99}))
    assert status == 409
