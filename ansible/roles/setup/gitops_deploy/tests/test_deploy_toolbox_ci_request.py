"""`fetch_ci_verdict`'s REQUEST path — the URL it builds, its headers, and its fail-closed error.

`test_deploy_toolbox.py` pins the two ends of the gate's binding (what `default_tools` binds,
what an unbound `DeployTools` answers) and `test_deploy_git_ci.py` pins the pure `ci_verdict`
reduction. Between them sat the part that actually talks to GitHub, covered by nothing: a call
that asked about the wrong commit, dropped the `Authorization` header, or let an outage escape
as `pass` would have been reported by no test in the suite.

`deploy_toolbox`'s docstring named this gap in prose ("the request path itself is covered by no
test"). This file closes it, so that sentence goes with it.

Every test drives the real function through a stubbed `urlopen`. `github_token` is stubbed with
it: the real one shells out to the GitHub CLI, which would make the asserted headers depend on
whether the runner happens to be logged in.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_toolbox_ci_request.py
"""

import json
import urllib.error
from email.message import Message

import pytest

import deploy_toolbox

SHA = "a" * 40
REPO = "DanielH2018/server"
CONTEXTS = frozenset({"lint", "test"})


def _stub_urlopen(monkeypatch, payload=None, error=None) -> list:
    """Replace `deploy_toolbox`'s urlopen and capture the Request objects it was handed."""
    captured: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def fake_urlopen(req, timeout=None):
        captured.append((req, timeout))
        if error is not None:
            raise error
        return _Resp()

    monkeypatch.setattr(deploy_toolbox.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(deploy_toolbox, "github_token", lambda *a, **k: "tok3n")
    return captured


def test_the_request_reads_the_check_runs_of_the_sha_it_was_asked_about(monkeypatch):
    """The URL, the timeout, the headers and the green reduction, in one real call.

    The SHA is asserted INSIDE the URL: the gate is supposed to read origin's verdict, and a
    request built from anything else would still return a plausible word.
    """
    captured = _stub_urlopen(
        monkeypatch,
        payload={
            "check_runs": [
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {"name": "test", "status": "completed", "conclusion": "success"},
            ]
        },
    )
    assert (
        deploy_toolbox.fetch_ci_verdict(
            SHA, require_ci=True, repo=REPO, contexts=CONTEXTS
        )
        == "pass"
    )
    req, timeout = captured[0]
    assert req.full_url == (
        f"https://api.github.com/repos/{REPO}/commits/{SHA}/check-runs?per_page=100"
    )
    assert timeout == 15
    # urllib capitalises header keys on the way in, so these are not the literals above.
    assert req.headers["Authorization"] == "Bearer tok3n"
    assert req.headers["User-agent"] == "gitops-deploy"
    assert req.headers["Accept"] == "application/vnd.github+json"


def test_a_failed_check_run_reaches_the_caller_as_fail(monkeypatch):
    """The verdict is read off the response body, not off a constant.

    Paired with the green case above: a `fetch_ci_verdict` hard-wired to either word fails one
    of the two, which is the only way to tell a working reduction from a lucky one.
    """
    _stub_urlopen(
        monkeypatch,
        payload={
            "check_runs": [
                {"name": "lint", "status": "completed", "conclusion": "failure"},
                {"name": "test", "status": "completed", "conclusion": "success"},
            ]
        },
    )
    assert (
        deploy_toolbox.fetch_ci_verdict(
            SHA, require_ci=True, repo=REPO, contexts=CONTEXTS
        )
        == "fail"
    )


def test_a_malformed_body_defers_rather_than_passing(monkeypatch):
    """A 200 carrying something that is not JSON is an unknown verdict, not a green one."""

    class _Garbage:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<html>502</html>"

    monkeypatch.setattr(
        deploy_toolbox.urllib.request, "urlopen", lambda req, timeout=None: _Garbage()
    )
    monkeypatch.setattr(deploy_toolbox, "github_token", lambda *a, **k: None)
    assert (
        deploy_toolbox.fetch_ci_verdict(
            SHA, require_ci=True, repo=REPO, contexts=CONTEXTS
        )
        == "pending"
    )


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("dns"),
        urllib.error.HTTPError("u", 503, "down", Message(), None),
        TimeoutError("slow"),
        OSError("reset"),
    ],
    ids=["urlerror", "httperror", "timeout", "oserror"],
)
def test_an_unreachable_github_defers_rather_than_passing(monkeypatch, error):
    """Fail closed on every error class the except clause claims to catch.

    The gate's contract is that an unknown verdict defers the tick and retries in 30 minutes;
    an outage reading as `pass` would ship an untested SHA. `HTTPError` is listed separately
    from the `URLError` it subclasses, so narrowing that clause to the parent alone still
    fails here rather than letting a 503 escape as a crash.
    """
    _stub_urlopen(monkeypatch, error=error)
    assert (
        deploy_toolbox.fetch_ci_verdict(
            SHA, require_ci=True, repo=REPO, contexts=CONTEXTS
        )
        == "pending"
    )


def test_a_disarmed_gate_passes_without_asking_github(monkeypatch):
    """`require_ci=False` short-circuits BEFORE the request, so it spends no API quota.

    The anonymous 60/hour limit is per source IP and shared with every landing's `await_ci.py`
    poll, so a disarmed gate that still fetched would be a real cost, not just a wasted call.
    """
    captured = _stub_urlopen(monkeypatch, payload={"check_runs": []})
    assert (
        deploy_toolbox.fetch_ci_verdict(
            SHA, require_ci=False, repo=REPO, contexts=CONTEXTS
        )
        == "pass"
    )
    assert captured == []
