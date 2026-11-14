"""The GitHub spend ledger, against a temporary directory, a fake clock and a fake `gh`.

Run: uv run pytest hosts/components/gh-quota/tests/test_gh_quota.py
"""

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from gh_quota import Ledger, Window, classify, main, render, session_key

NOW = 1_794_668_400.0  # 15:00:00 UTC
RESET = int(NOW) + 1200
SESSION = "7c1e90aa-0000-4000-8000-000000000001"


class FakeGh:
    """Stands in for `gh api rate_limit`: answers that one call, or fails the way `gh` can."""

    def __init__(self):
        self.graphql_used = 100
        self.core_used = 0
        self.fail: Exception | None = None
        self.env: dict[str, str] = {}

    def __call__(self, argv, *, env, **_kwargs):
        assert argv == ["gh", "api", "rate_limit"]
        self.env = env
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

    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
    (path,) = ledger.root.glob("*.sqlite")
    assert path.name.startswith(f"tok-{fingerprint}@")
    assert gh.env["GH_TOKEN"] == token


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
