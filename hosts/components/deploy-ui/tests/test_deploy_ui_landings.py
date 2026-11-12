"""The landing viewer: the log index, the parser, and the two routes that read them.

The fixture log is PR #7827's real landing, cut down: the 120-name promotion line keeps its
head, and the 42 identical host-wait lines keep three. The index tests pass a pinned `now`
and date their files against it; the route tests order files by their own stat times.
"""

import json
import os
import subprocess

import pytest
from land_lib.outcome import VERDICTS

import deploy_ui
from deploy_ui_lib import deploy_ui_reads as reads

NOW = 1_762_700_000.0
DAY = 24 * 3600
LOG_7827 = """\
== arm  arming PR #7827's merge
  PR #7827: not arming auto-merge — merging directly once CI is green, at the head CI checked
== 0/6  waiting for PR #7827 to merge (merging it directly once its CI is green)
  merged directly at the checked head: Link PR numbers and show each landing's PR title on the deploy page
  merged after 720s
== 1/6  resolving PR #7827
  merge commit 7f3a78f6f5c0b76626222814a5daaed21f0f6dcb
== 2/6  pre-flight: can the tick cross what is incoming?
warning: `VIRTUAL_ENV=.claude/worktrees/deploy-ui-pr-links/.venv` does not match the project environment path `.venv` and will be ignored
  nothing in the way
== 3/6  waiting for master CI
7f3a78f6: CI green
== 4/6  waiting on the promotion
  promote.py land-wait: waiting on artifacts (at a471befd, before this merge), authelia (at a471befd, before this merge)
  promoted: 1 Kustomizations applied 7f3a78f6
== 5/6  waiting for hosts to apply
  waiting for hosts to apply: daniel-box
  waiting for hosts to apply: daniel-box
  waiting for hosts to apply: daniel-box
VERDICT: deferred (PR #7827, 7f3a78f6f5c0 — daniel-box has not applied it by 2026-11-09 16:47 UTC)
"""
CLI_NAME = "land7827-20261109-155944.log"


class Ps:
    """A runner answering `ps` with `self.out` and every other command with nothing."""

    def __init__(self) -> None:
        self.out = ""

    def __call__(self, argv, **kw):
        out = self.out if argv[0] == "ps" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def app(tmp_path):
    for d in ("logs", "cli", "locks"):
        (tmp_path / d).mkdir()
    (tmp_path / "proc_locks").write_text("")
    (tmp_path / "cli" / CLI_NAME).write_text(LOG_7827)
    cfg = deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path / "logs",
        bind="127.0.0.1",
        port=0,
        tree_lock=tmp_path / "locks" / "tree.lock",
        lock_dir=tmp_path / "locks",
        proc_locks=tmp_path / "proc_locks",
        cli_log_dir=tmp_path / "cli",
    )
    return deploy_ui.App(cfg, run=Ps())


def body(resp):
    return json.loads(resp[1])


def _dated(path, text, when):
    path.write_text(text)
    os.utime(path, (when, when))
    return path


def test_parse_landing_reads_stages_and_verdict_is_clean():
    parsed = reads.parse_landing(LOG_7827)
    assert parsed["pr"] == "7827" and parsed["of"] == 6
    assert [s["step"] for s in parsed["stages"]] == [
        "arm",
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    v = parsed["verdict"]
    assert (v["verdict"], v["tone"]) == ("deferred", "warn")
    assert v["detail"].startswith("PR #7827, 7f3a78f6f5c0")


def test_parse_landing_folds_repeats_and_keeps_unindented_lines_is_clean():
    stages = {s["step"]: s["lines"] for s in reads.parse_landing(LOG_7827)["stages"]}
    assert stages["5"] == [["waiting for hosts to apply: daniel-box", 3]]
    assert stages["3"] == [["7f3a78f6: CI green", 1]]


def test_parse_landing_keeps_an_unnumbered_header_inside_its_stage_is_flagged():
    text = "== 3/6  waiting for master CI\n== handing PR #1 to land-1.service\n"
    stages = reads.parse_landing(text)["stages"]
    assert len(stages) == 1
    assert stages[0]["lines"] == [["== handing PR #1 to land-1.service", 1]]


def test_parse_landing_without_a_verdict_is_still_running_is_flagged():
    parsed = reads.parse_landing(LOG_7827.rsplit("VERDICT", 1)[0])
    assert parsed["verdict"] is None and parsed["stages"][-1]["step"] == "5"


def test_every_land_verdict_has_a_hint_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing logs
    assert set(reads.VERDICT_HINTS) == {str(v) for v in VERDICTS}
    assert {t for t, _ in reads.VERDICT_HINTS.values()} <= {"ok", "none", "warn", "bad"}


@pytest.mark.parametrize(
    "name", [CLI_NAME, "land-20261109T165126Z-k3x_9q.log"], ids=["cli", "page"]
)
def test_landing_log_takes_both_name_forms_is_clean(tmp_path, name):
    assert reads.landing_log(_dated(tmp_path / name, "x", NOW), DAY, NOW)


@pytest.mark.parametrize(
    "name", ["land7827-20261109-155944.pid", "notes.log", "land.log"]
)
def test_landing_log_refuses_other_names_is_flagged(tmp_path, name):
    assert not reads.landing_log(_dated(tmp_path / name, "x", NOW), DAY, NOW)


def test_landing_log_refuses_a_symlink_and_an_old_file_is_flagged(tmp_path):
    secret = _dated(tmp_path / "secret", "x", NOW)
    (tmp_path / CLI_NAME).symlink_to(secret)
    old = _dated(tmp_path / "land1-20200101-000000.log", "x", NOW - DAY - 1)
    assert not reads.landing_log(tmp_path / CLI_NAME, DAY, NOW)
    assert not reads.landing_log(old, DAY, NOW)


def test_log_route_serves_a_cli_landing_by_name_is_clean(app):
    b = body(app.get(f"/api/log?name={CLI_NAME}"))
    assert b["pr"] == "7827" and b["verdict"]["verdict"] == "deferred"
    assert b["raw"] == LOG_7827 and b["truncated"] is False


def test_log_route_answers_unchanged_while_the_size_holds_is_clean(app):
    size = body(app.get(f"/api/log?name={CLI_NAME}"))["size"]
    b = body(app.get(f"/api/log?name={CLI_NAME}&size={size}"))
    assert b == {"name": CLI_NAME, "size": size, "unchanged": True}


@pytest.mark.parametrize(
    "name", ["../cli/" + CLI_NAME, "/etc/passwd", "land9-20261109-000000.log"]
)
def test_log_route_refuses_a_name_it_did_not_list_is_flagged(app, name):
    b = body(app.get(f"/api/log?name={name}"))
    assert "no landing log named" in b["unavailable"] and "raw" not in b


def test_log_route_reads_an_inflight_landing_wherever_it_logs_is_clean(app, tmp_path):
    """A session's landing logs under its own job dir, which no directory scan covers."""
    log = tmp_path / "job" / "land1550-20261109-170000.log"
    log.parent.mkdir()
    with log.open("w") as out:
        out.write("== arm  arming PR #1550's merge\n")
        out.flush()
        proc = subprocess.Popen(["sleep", "30"], stdout=out)
    try:
        assert "unavailable" in body(app.get(f"/api/log?name={log.name}"))
        app.run.out = f"{proc.pid} 1 5 /x/python3 land.py --pr 1550 --since 72c1a41\n"
        rows = body(app.get("/api/inflight"))["runs"]
        assert [(r["kind"], r["log"]) for r in rows] == [("land", str(log))]
        assert body(app.get(f"/api/log?name={log.name}"))["pr"] == "1550"
    finally:
        proc.kill()
        proc.wait()


def test_inflight_landing_writing_to_a_pipe_is_not_indexed_is_flagged(app):
    """A foreground landing's stdout is a terminal; a pipe fails the same regular-file check."""
    with subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE) as proc:
        try:
            app.run.out = f"{proc.pid} 1 5 /x/python3 land.py --pr 1550 --since 1a\n"
            app.get("/api/inflight")
            names = [r["name"] for r in body(app.get("/api/landings"))["landings"]]
            assert names == [CLI_NAME]
        finally:
            proc.kill()


def test_landings_lists_the_window_newest_first_is_clean(app, tmp_path):
    cli = tmp_path / "cli" / CLI_NAME
    newer = tmp_path / "logs" / "land-20261109T180000Z-zz.log"
    _dated(newer, "== arm  arming PR #9's merge\n", cli.stat().st_mtime + 60)
    rows = body(app.get("/api/landings"))["landings"]
    assert [(r["name"], r["pr"]) for r in rows] == [
        (newer.name, "9"),
        (CLI_NAME, "7827"),
    ]
    assert rows[0]["verdict"] is None and rows[1]["verdict"]["verdict"] == "deferred"


def test_landings_skips_a_log_older_than_the_window_is_flagged(app, tmp_path):
    os.utime(tmp_path / "cli" / CLI_NAME, (0, 0))
    assert body(app.get("/api/landings")) == {"landings": []}
