"""The GitHub spend ledger, against a temporary directory, a fake clock and a fake `gh`.

Run: uv run pytest hosts/components/gh-quota/tests/test_gh_quota.py
"""

import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest
from gh_quota import KEEP_S, Ledger, Window, classify, main, render, session_key
from lib.proc_testing import write_exec

NOW = 1_794_668_400.0  # 15:00:00 UTC
RESET = int(NOW) + 1200
SESSION = "7c1e90aa-0000-4000-8000-000000000001"


class FakeGh:
    """Stands in for `gh`: names its stored token and answers `gh api rate_limit`.

    `logins` maps a `GH_CONFIG_DIR`, None when the caller sets none, to the token
    `gh auth token` prints there. A directory it does not name is not logged in.
    `fail` makes the rate-limit call fail the way `gh` can.
    """

    def __init__(self):
        self.logins: dict[str | None, str] = {None: "gho_operator"}
        self.graphql_used = 100
        self.core_used = 0
        self.fail: Exception | None = None
        self.env: dict[str, str] = {}

    def __call__(self, argv, *, env, check=False, **_kwargs):
        self.env = env
        if argv == ["gh", "auth", "token"]:
            token = self.logins.get(env.get("GH_CONFIG_DIR"))
            if token is not None:
                return subprocess.CompletedProcess(argv, 0, f"{token}\n", "")
            if check:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 1, "", "no oauth token found\n")
        assert argv == ["gh", "api", "rate_limit"]
        if self.fail is not None:
            raise self.fail
        resources = {
            "graphql": {"used": self.graphql_used, "limit": 5000, "reset": RESET},
            "core": {"used": self.core_used, "limit": 5000, "reset": RESET + 60},
        }
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"resources": resources}), ""
        )


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self) -> float:
        return self.t


def _ledger(
    tmp_path: Path, mode: str, env: dict[str, str]
) -> tuple[Ledger, Clock, FakeGh]:
    conf = tmp_path / "gh-quota.conf"
    conf.write_text(f"mode={mode}\n")
    root = tmp_path / "ledger"
    root.mkdir()
    clock, gh = Clock(), FakeGh()
    return Ledger(root=root, conf=conf, env=env, now=clock, run=gh), clock, gh


def _identity_of(token: str) -> str:
    return "tok-" + hashlib.sha256(token.encode()).hexdigest()[:12]


@pytest.fixture
def recording(tmp_path: Path):
    """A ledger in `record` mode, with the pieces a test moves: (ledger, clock, gh)."""
    return _ledger(tmp_path, "record", {"CLAUDE_CODE_SESSION_ID": SESSION})


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
    ledger, _clock, _gh = _ledger(tmp_path, "off", {})

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


def test_a_token_caller_gets_a_ledger_named_by_fingerprint(tmp_path: Path):
    token = "ghp_notarealtoken"
    ledger, _clock, gh = _ledger(tmp_path, "record", {"GH_TOKEN": token})

    ledger.record(["pr", "list"])

    (path,) = ledger.root.glob("*.sqlite")
    assert path.name.startswith(f"{_identity_of(token)}@")
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
        _identity_of("gho_operator"): {SESSION: 1},
        _identity_of("gho_agent"): {"agent": 2},
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
    ledger, _clock, gh = _ledger(tmp_path, "record", env)
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
    ledger, _clock, gh = _ledger(tmp_path, "record", {})
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
    ledger, _clock, gh = _ledger(tmp_path, "record", {})
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

    assert render([window], failures=0, every=False) == [
        count + "; --all lists 1 more",
        loud,
    ]
    assert render([window], failures=0, every=True) == [
        count,
        loud,
        "  quiet  12 calls  pr view x12",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("gh is not installed"),
        subprocess.CalledProcessError(1, ["gh", "api", "rate_limit"]),
    ],
    ids=["gh-missing", "gh-exits-nonzero"],
)
def test_a_failed_sample_is_counted_and_the_call_is_still_recorded(recording, failure):
    ledger, clock, gh = recording
    gh.fail = failure

    ledger.record(["pr", "list"])

    assert ledger.failures() == 1
    gh.fail = None
    clock.t += 31
    (window,) = ledger.windows()
    assert window.sessions == {SESSION: {"pr list": 1}}


def test_record_lets_the_call_through_when_the_ledger_file_is_corrupt(recording):
    ledger, _clock, _gh = recording
    ledger.record(["pr", "list"])
    (path,) = ledger.root.glob("*.sqlite")
    path.write_bytes(
        b"not a database, and longer than a SQLite header needs to be to say so"
    )

    ledger.record(["pr", "list"])

    assert ledger.failures() == 1


def test_a_failure_older_than_the_ledger_keeps_its_rows_is_not_counted(recording):
    ledger, clock, gh = recording
    gh.fail = OSError("gh is not installed")
    ledger.record(["pr", "list"])
    gh.fail = None

    clock.t += KEEP_S - 60
    assert ledger.failures() == 1
    clock.t += 120
    assert ledger.failures() == 0


def test_a_failure_log_at_its_size_limit_drops_its_old_lines_and_takes_the_new_one(
    recording,
):
    ledger, _clock, gh = recording
    log = ledger.root / f"failures.{os.getuid()}"
    recent = f"{int(NOW) - 60} OSError: disk full"
    log.write_text(
        f"{int(NOW) - KEEP_S - 60} OSError: disk full\n" * 4000 + f"{recent}\n"
    )
    gh.fail = OSError("gh is not installed")

    ledger.record(["pr", "list"])

    assert log.read_text().splitlines() == [
        recent,
        f"{int(NOW)} {SESSION} OSError: gh is not installed",
    ]


def test_a_failure_log_full_of_recent_lines_takes_no_new_line(recording):
    ledger, _clock, gh = recording
    log = ledger.root / f"failures.{os.getuid()}"
    full = f"{int(NOW) - 60} OSError: disk full\n" * 4000
    log.write_text(full)
    gh.fail = OSError("gh is not installed")

    ledger.record(["pr", "list"])

    assert log.read_text() == full


def test_the_command_prints_status_as_one_line_of_json(recording, capsys):
    ledger, _clock, _gh = recording
    ledger.record(["pr", "list"])

    assert main(["status", "--json"], ledger) == 0

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    (row,) = json.loads(out)
    assert (row["bucket"], row["used"], row["recorded"], row["beyond"]) == (
        "graphql",
        100,
        1,
        99,
    )
    assert row["sessions"] == {SESSION: {"pr list": 1}}


def test_the_command_says_so_when_the_host_does_not_record(tmp_path: Path, capsys):
    ledger, _clock, _gh = _ledger(tmp_path, "off", {})

    assert main(["mode"], ledger) == 0
    assert main(["status"], ledger) == 0

    mode, status = capsys.readouterr().out.splitlines()
    assert mode == "off"
    assert "gh-quota is off on this host" in status
