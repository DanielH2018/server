"""The GitHub spend ledger, against a temporary directory, a fake clock and a fake `gh`.

Run: uv run pytest hosts/components/gh-quota/tests/test_gh_quota.py
"""

import dataclasses
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest
from gh_quota import Ledger, Window, classify, is_write, session_key
from gh_quota_cli import main, render
from lib.proc_testing import write_exec

from _gh_quota_fakes import RESET, SESSION, identity_of, make_ledger


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pr", "list", "--state", "open"], ("graphql", "pr list")),
        (["run", "view", "4567", "--log-failed"], ("core", "run view")),
        (
            ["api", "graphql", "-f", "query={ viewer { login } }"],
            ("graphql", "api graphql"),
        ),
        (["api", "--jq", ".x", "graphql"], ("graphql", "api graphql")),
        (
            ["api", "graphql", "-f", "query=mutation { addComment(input: {}) { id } }"],
            ("graphql", "api mutation"),
        ),
        (["api", "repos/o/r/pulls", "--paginate"], ("core", "api rest")),
        (
            ["api", "repos/o/r/issues/1/comments", "-f", "body=hi"],
            ("core", "api write"),
        ),
        (["api", "-XDELETE", "repos/o/r/git/refs/heads/x"], ("core", "api write")),
        (
            ["api", "-X", "GET", "search/issues", "-f", "q=is:open"],
            ("core", "api rest"),
        ),
        (["api", "rate_limit", "--jq", ".resources.core.reset"], None),
        (["auth", "token"], None),
        (["--version"], None),
    ],
)
def test_classify_names_the_bucket_and_the_call_class(argv, expected):
    assert classify(argv) == expected


@pytest.mark.parametrize(
    ("argv", "writes"),
    [
        (["pr", "create", "--title", "t"], True),
        (["pr", "ready", "123"], True),
        (["issue", "comment", "9", "--body", "x"], True),
        (["api", "repos/o/r/issues/1/comments", "-f", "body=hi"], True),
        (["api", "graphql", "-f", "query=mutation { a }"], True),
        (["pr", "view", "123"], False),
        (["pr", "checkout", "123"], False),
        (["run", "list"], False),
        (["api", "repos/o/r/pulls"], False),
    ],
)
def test_a_call_that_creates_content_is_a_write(argv, writes):
    call = classify(argv)
    assert call is not None
    assert is_write(call[1]) is writes


def test_a_call_is_counted_against_the_claude_session_before_the_service(
    tmp_path: Path,
):
    service = tmp_path / "service"
    service.write_text("0::/system.slice/claude-land@8705.service\n")
    login = tmp_path / "login"
    login.write_text("0::/user.slice/user-1000.slice/session-12.scope\n")

    assert session_key({"CLAUDE_CODE_SESSION_ID": SESSION}, service) == SESSION
    assert session_key({}, service) == "claude-land@8705.service"
    assert session_key({}, login) == "operator"
    assert session_key({}, tmp_path / "missing") == "operator"


def test_record_counts_each_call_against_its_session(recording):
    ledger, _clock, gh = recording
    gh.core_used = 7
    for _ in range(3):
        ledger.record(["issue", "list", "--label", "claude"])
    ledger.record(["pr", "view", "123"])
    ledger.record(["run", "list"])
    ledger.record(["auth", "status"])

    graphql, core = ledger.windows()
    assert (graphql.bucket, graphql.reset, graphql.used) == ("graphql", RESET, 100)
    assert graphql.sessions == {SESSION: {"issue list": 3, "pr view": 1}}
    assert (core.bucket, core.reset, core.used) == ("core", RESET + 60, 7)
    assert core.sessions == {SESSION: {"run list": 1}}


def test_record_writes_nothing_when_the_host_does_not_record(tmp_path: Path):
    ledger, _clock, _gh = make_ledger(tmp_path, "off", {})

    ledger.record(["pr", "list"])

    assert list(ledger.root.iterdir()) == []


def test_github_is_sampled_once_per_interval(recording):
    ledger, clock, gh = recording
    ledger.record(["pr", "list"])

    clock.t += 29
    gh.graphql_used = 180
    ledger.record(["pr", "list"])
    (window,) = ledger.windows()
    assert window.used == 100

    clock.t += 2
    ledger.record(["pr", "list"])
    (window,) = ledger.windows()
    assert window.used == 180


def test_a_reset_reported_seconds_later_is_the_same_window(recording):
    ledger, clock, gh = recording
    ledger.record(["pr", "list"])

    clock.t += 31
    gh.graphql_reset = RESET + 45
    gh.graphql_used = 180
    ledger.record(["pr", "list"])

    (window,) = ledger.windows()
    assert (window.reset, window.used) == (RESET, 180)


def test_status_and_new_calls_follow_the_freshest_sample_not_the_latest_reset(
    recording,
):
    ledger, clock, gh = recording
    gh.graphql_reset, gh.graphql_used = RESET + 1260, 1853
    ledger.record(["pr", "list"])

    clock.t += 31
    gh.graphql_reset, gh.graphql_used = RESET, 2219
    ledger.record(["pr", "list"])
    ledger.record(["pr", "view"])

    (window,) = ledger.windows()
    assert (window.reset, window.used) == (RESET, 2219)
    (path,) = ledger.root.glob("*.sqlite")
    with closing(sqlite3.connect(path)) as db:
        spend = db.execute(
            "SELECT window, argv_class, n FROM spend ORDER BY window, argv_class"
        ).fetchall()
    assert spend == [
        (RESET, "pr list", 1),
        (RESET, "pr view", 1),
        (RESET + 1260, "pr list", 1),
    ]


def test_a_token_caller_gets_a_ledger_named_by_fingerprint(tmp_path: Path):
    token = "ghp_notarealtoken"
    ledger, _clock, gh = make_ledger(tmp_path, "record", {"GH_TOKEN": token})

    ledger.record(["pr", "list"])

    (path,) = ledger.root.glob("*.sqlite")
    assert path.name.startswith(f"{identity_of(token)}@")
    assert gh.env["GH_TOKEN"] == token


def test_two_logins_of_one_uid_are_counted_in_separate_ledgers(recording):
    operator, _clock, gh = recording
    gh.logins["/home/agent/gh"] = "gho_agent"
    agent = dataclasses.replace(
        operator,
        env={"CLAUDE_CODE_SESSION_ID": "agent", "GH_CONFIG_DIR": "/home/agent/gh"},
    )

    operator.record(["pr", "list"])
    agent.record(["pr", "list"])
    agent.record(["issue", "list"])

    calls = {
        w.identity: {session: sum(n.values()) for session, n in w.sessions.items()}
        for w in operator.windows()
        if w.bucket == "graphql"
    }
    assert calls == {
        identity_of("gho_operator"): {SESSION: 1},
        identity_of("gho_agent"): {"agent": 2},
    }


def test_each_sample_names_its_caller_by_session_gh_path_and_variable_names(
    tmp_path: Path,
):
    bin_dir = tmp_path / "bin"
    real = write_exec(tmp_path / "real-gh", "#!/bin/sh\n")
    bin_dir.mkdir()
    (bin_dir / "gh").symlink_to(real)
    token = "ghp_notarealtoken"
    env = {
        "CLAUDE_CODE_SESSION_ID": SESSION,
        "PATH": str(bin_dir),
        "GH_TOKEN": token,
        "GITHUB_REPOSITORY": "o/r",
        "HOME": "/home/x",
    }
    ledger, _clock, gh = make_ledger(tmp_path, "record", env)
    gh.core_used = 7

    ledger.record(["pr", "list"])

    (path,) = ledger.root.glob("*.sqlite")
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT window, bucket, session, gh, env_names FROM sampler ORDER BY bucket"
        ).fetchall()
    names = "GH_TOKEN,GITHUB_REPOSITORY"
    assert rows == [
        (RESET + 60, "core", SESSION, str(real), names),
        (RESET, "graphql", SESSION, str(real), names),
    ]
    assert token not in path.read_bytes().decode(errors="replace")


def _sampler_process_rows(ledger: Ledger) -> list[tuple]:
    (path,) = ledger.root.glob("*.sqlite")
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            "SELECT window, bucket, cgroup, ancestry FROM sampler_process ORDER BY bucket"
        ).fetchall()


def test_each_sample_names_its_process_by_cgroup_and_ancestor_comm_names(
    tmp_path: Path,
):
    proc = tmp_path / "proc"
    stats = {
        "self": "4242 (python3) S 4100 4242 0",
        "4100": "4100 (bash) S 3000 4100 0",
        "3000": "3000 (tmux: server) S 1 3000 0",
        "1": "1 (systemd) S 0 1 0",
    }
    for pid, stat in stats.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "stat").write_text(stat + "\n")
    cgroup = "0::/user.slice/user-1000.slice/user@1000.service/tmux-spawn-1.scope"
    (proc / "self" / "cgroup").write_text(cgroup + "\n")
    ledger, _clock, gh = make_ledger(tmp_path, "record", {})
    ledger = dataclasses.replace(ledger, proc=proc)
    gh.core_used = 7

    ledger.record(["pr", "list"])

    ancestry = "python3<bash<tmux: server"
    assert _sampler_process_rows(ledger) == [
        (RESET + 60, "core", cgroup, ancestry),
        (RESET, "graphql", cgroup, ancestry),
    ]


def test_a_sample_whose_procfs_cannot_be_read_stores_empty_process_facts(
    recording, tmp_path: Path
):
    ledger, _clock, _gh = recording
    ledger = dataclasses.replace(ledger, proc=tmp_path / "no-proc")

    ledger.record(["pr", "list"])

    assert _sampler_process_rows(ledger) == [(RESET, "graphql", "", "")]
    assert ledger.failures() == 0


def test_a_comm_cut_inside_a_multibyte_character_does_not_fail_the_sample(
    recording, tmp_path: Path
):
    ledger, _clock, _gh = recording
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "stat").write_bytes(b"4242 (ab\xe2\x82) S 1 4242 0\n")
    ledger = dataclasses.replace(ledger, proc=proc)

    ledger.record(["pr", "list"])

    assert _sampler_process_rows(ledger) == [(RESET, "graphql", "", "ab�")]
    assert ledger.failures() == 0


def test_a_caller_whose_gh_has_no_token_gets_a_ledger_named_by_uid(tmp_path: Path):
    ledger, _clock, gh = make_ledger(tmp_path, "record", {})
    gh.logins.clear()

    ledger.record(["pr", "list"])

    (path,) = ledger.root.glob("*.sqlite")
    assert path.name.startswith(f"uid-{os.getuid()}@")


def test_status_lists_only_the_sessions_over_the_share_until_asked_for_all():
    window = Window(
        identity="uid-1000",
        bucket="graphql",
        used=3120,
        quota=5000,
        reset=RESET,
        sessions={
            "loud": Counter({"issue list": 960, "pr view": 40}),
            "quiet": Counter({"pr view": 12}),
        },
    )
    count = (
        "uid-1000 graphql: 3120/5000 used, resets 15:20 UTC; 2 sessions made 1012 calls; "
        "2108 beyond the ledger (68%); 1 at or over 20%"
    )
    loud = "  loud  1000 calls  issue list x960, pr view x40"

    assert render([window], [], failures=0, every=False) == [
        count + "; --all lists 1 more",
        loud,
    ]
    assert render([window], [], failures=0, every=True) == [
        count,
        loud,
        "  quiet  12 calls  pr view x12",
    ]


def test_the_command_prints_status_as_one_line_of_json(recording, capsys):
    ledger, _clock, _gh = recording
    ledger.record(["pr", "list"])
    ledger.record(["pr", "ready", "123"])

    assert main(["status", "--json"], ledger) == 0

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    (row,) = json.loads(out)
    assert (row["bucket"], row["used"], row["recorded"], row["beyond"]) == (
        "graphql",
        100,
        2,
        98,
    )
    assert row["sessions"] == {SESSION: {"pr list": 1, "pr ready": 1}}
    assert (row["writes_minute"], row["writes_hour"]) == (1, 1)


def test_status_prints_recent_writes_against_the_secondary_limits(recording, capsys):
    ledger, clock, _gh = recording
    for _ in range(3):
        ledger.record(["pr", "create", "--fill"])
    clock.t += 61
    ledger.record(["issue", "comment", "9", "--body", "x"])
    ledger.record(["pr", "view", "123"])
    clock.t += 3540
    ledger.record(["pr", "merge", "123"])

    assert main(["status"], ledger) == 0

    identity = identity_of("gho_operator")
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"{identity} writes: 1/80 in the last minute, 2/500 in the last hour"
    )


def test_the_command_says_so_when_the_host_does_not_record(tmp_path: Path, capsys):
    ledger, _clock, _gh = make_ledger(tmp_path, "off", {})

    assert main(["mode"], ledger) == 0
    assert main(["status"], ledger) == 0

    mode, status = capsys.readouterr().out.splitlines()
    assert mode == "off"
    assert "gh-quota is off on this host" in status
