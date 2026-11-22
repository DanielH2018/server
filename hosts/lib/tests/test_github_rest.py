import hashlib
import json
import subprocess
from pathlib import Path

import github_rest
from gh_quota import Ledger


class _Body:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok":true}'


# One lookup for every host GitHub reader. The anonymous limit is 60/hour per source
# IP and shared by every caller on the host, so each rule is an accept/reject pair: a resolver
# that always returned a token and one that never did are indistinguishable from one side.


class _GhProc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_github_token_from_the_source_wins_without_running_gh():
    def never(*_a, **_k):
        raise AssertionError("gh must not run when the source carries a token")

    assert github_rest.github_token({"GH_TOKEN": "ghp_env"}, never) == "ghp_env"
    assert github_rest.github_token({"GITHUB_TOKEN": "ghp_cfg"}, never) == "ghp_cfg"


def test_github_token_falls_back_to_gh_auth_token():
    calls = []

    def run(cmd, **_k):
        calls.append(cmd)
        return _GhProc(0, "gho_cli\n")

    assert github_rest.github_token({}, run) == "gho_cli"
    assert calls == [["gh", "auth", "token"]]


def test_github_token_with_no_token_anywhere_is_anonymous():
    """A logged-out gh, a missing binary or a blank value must degrade to anonymous."""
    assert github_rest.github_token({}, lambda *_a, **_k: _GhProc(1, "")) is None
    assert github_rest.github_token({}, lambda *_a, **_k: _GhProc(0, "  \n")) is None

    def boom(*_a, **_k):
        raise FileNotFoundError("gh")

    assert github_rest.github_token({"GH_TOKEN": "   "}, boom) is None


def test_github_get_sends_the_token_and_parses_the_body():
    captured = {}

    class _Json(_Body):
        def read(self, *_a):
            return b'{"check_runs": []}'

    def opener(req, timeout=None):
        captured["req"], captured["timeout"] = req, timeout
        return _Json()

    body = github_rest.github_get(
        "repos/o/n/pulls", "tok", user_agent="ua", opener=opener
    )
    assert body == {"check_runs": []}
    req = captured["req"]
    assert req.full_url == "https://api.github.com/repos/o/n/pulls"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("User-agent") == "ua"
    assert captured["timeout"] == github_rest.GITHUB_TIMEOUT_S


def test_github_get_without_a_token_sends_no_authorization_header():
    captured = {}

    class _Json(_Body):
        def read(self, *_a):
            return b"[]"

    def opener(req, timeout=None):
        captured["req"] = req
        return _Json()

    github_rest.github_get("repos/o/n/issues", None, user_agent="ua", opener=opener)
    assert captured["req"].get_header("Authorization") is None


def test_github_get_counts_an_authenticated_read_under_the_token_it_sends(
    monkeypatch, tmp_path
):
    gh_quota_module = (
        Path(__file__).parents[2] / "components/gh-quota/files/gh_quota.py"
    )
    monkeypatch.setenv("GH_QUOTA_MODULE", str(gh_quota_module))
    monkeypatch.setenv("GH_QUOTA_MODE", "record")
    monkeypatch.setenv("GH_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "the-sessions-own-token")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-under-test")
    resources = {
        "graphql": {"used": 0, "limit": 5000, "reset": 0},
        "core": {"used": 40, "limit": 5000, "reset": 4_000_000_000},
    }

    def sample(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"resources": resources}), ""
        )

    # Reading the ledger first takes the sample, so the reads below run none of their own.
    ledger = Ledger(root=tmp_path, env={"GH_TOKEN": "builder-token"}, run=sample)
    (window,) = ledger.windows()
    assert window.sessions == {}

    class _Json(_Body):
        def read(self, *_a):
            return b"{}"

    for path, token in [
        ("repos/o/n/commits/abc/check-runs?per_page=100", "builder-token"),
        ("repos/o/n/commits/abc/check-runs?per_page=100", None),
        ("rate_limit", "builder-token"),
    ]:
        github_rest.github_get(
            path, token, user_agent="ua", opener=lambda *_a, **_k: _Json()
        )

    (window,) = ledger.windows()
    assert window.identity == "tok-" + hashlib.sha256(b"builder-token").hexdigest()[:12]
    assert window.sessions == {"session-under-test": {"api rest": 1}}
