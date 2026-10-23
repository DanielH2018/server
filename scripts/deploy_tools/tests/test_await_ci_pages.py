"""`await_ci`'s check-runs and check-suites reads walk every page (#5977).

One page of 100 hid a run past the 100th, so a landing could read a red commit as green.
`deploy_git.read_check_pages` holds the walk; this pins that `await_ci` reaches it through the
real request path, with `urlopen` replaced on the stdlib so no first-party module is patched.

Run: uv run pytest scripts/deploy_tools/tests/test_await_ci_pages.py
"""

import json
import urllib.request

import await_ci

REQUIRED = frozenset({"prek (lint + validate + tests + secrets)"})


def _stub_two_pages(monkeypatch, key: str, page_two: list[dict], total=None):
    """A full page 1 under `key`, then `page_two`, by the URL's `page`.

    Page 1 ends in a passing required run, so a one-page read of it is a false green.
    """
    required = {
        "name": next(iter(REQUIRED)),
        "status": "completed",
        "conclusion": "success",
    }
    page_one = [{"name": f"filler-{i}"} for i in range(99)] + [required]
    count = 100 + len(page_two) if total is None else total

    class _Resp:
        def __init__(self, entries):
            self._body = json.dumps({"total_count": count, key: entries})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body.encode()

    def fake_urlopen(req, timeout=None):
        return _Resp(page_two if req.full_url.endswith("&page=2") else page_one)

    monkeypatch.setenv("GH_TOKEN", "tok3n")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_a_failing_run_on_the_second_page_fails_the_wait(monkeypatch):
    failing = {
        "name": next(iter(REQUIRED)),
        "status": "completed",
        "conclusion": "failure",
    }
    _stub_two_pages(monkeypatch, "check_runs", [failing])
    assert await_ci.verdict_for("a" * 40, required=REQUIRED) == "fail"


def test_a_read_short_of_total_count_is_pending(monkeypatch):
    passing = {
        "name": next(iter(REQUIRED)),
        "status": "completed",
        "conclusion": "success",
    }
    _stub_two_pages(monkeypatch, "check_runs", [passing], total=150)
    assert await_ci.verdict_for("a" * 40, required=REQUIRED) == "pending"


def test_a_check_suite_on_the_second_page_is_read(monkeypatch):
    suite = {"app": {"slug": "github-actions"}}
    _stub_two_pages(monkeypatch, "check_suites", [suite])
    assert suite in await_ci.fetch_check_suites("a" * 40)
