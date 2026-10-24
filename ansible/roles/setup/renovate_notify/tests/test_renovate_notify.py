import json
import urllib.parse
import urllib.request

import renovate_notify as rn


class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_discord_post_sends_user_agent(monkeypatch):
    """Discord sits behind Cloudflare, which 403s the default Python-urllib User-Agent
    (error code 1010). discord() must set an explicit User-Agent or every post silently
    fails (the helper swallows the error, so the run still exits 0)."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rn.discord("https://discord.com/api/webhooks/1/abc", "hello")
    assert captured.get("req") is not None, "discord() never issued a request"
    assert captured["req"].get_header("User-agent"), (
        "discord() POST must set a User-Agent (Cloudflare 403s the urllib default)"
    )


def test_discord_returns_true_on_2xx(monkeypatch):
    # A confirmed delivery returns True so the caller persists the dedupe fingerprint.
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(204)
    )
    assert rn.discord("https://discord.com/api/webhooks/1/abc", "hi") is True


def test_discord_returns_false_on_error(monkeypatch):
    # A transient webhook failure must return False so the fingerprint is NOT advanced and the
    # digest is retried next run, instead of being permanently suppressed.
    def boom(req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert rn.discord("https://discord.com/api/webhooks/1/abc", "hi") is False


def test_an_over_long_digest_reaches_the_webhook_ending_in_the_truncation_marker(
    monkeypatch,
):
    # Two caps used to apply in sequence, and the second sliced off the first's marker, so a
    # cut digest read as a complete one (#3351).
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["content"] = json.loads(req.data)["content"]
        return _FakeResp(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rn.discord("https://discord.com/api/webhooks/1/abc", "x" * 5000)
    assert len(captured["content"]) <= 2000
    assert captured["content"].endswith("…(truncated)")


def test_discord_returns_false_without_webhook():
    assert rn.discord("", "hi") is False


# --- #6002: the check-runs read walks every page and refuses a short one ---------------------
#
# GitHub answers an unparameterised check-runs GET with its default page of 30. The fake below
# honours `per_page` (default 30, cap 100) and `page` the way the API does, so only a reader
# that walks the pages sees a run past the first 30. It stubs urlopen, the transport, so the
# read is caught whether it goes through `rn.get` or a shared walk in host_lib.

_SHA = "c0ffee"
_OPEN_PR = {
    "number": 7,
    "title": "chore(deps): update thing",
    "html_url": "https://github.com/o/r/pull/7",
    "body": "",
    "created_at": "2026-10-01T00:00:00Z",
    "head": {"sha": _SHA, "ref": "renovate/thing"},
    "user": {"login": "renovate[bot]"},
}


class _JsonResp:
    def __init__(self, body):
        self._raw = json.dumps(body).encode()

    def read(self, *a):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(i: int, conclusion: str = "success") -> dict:
    return {
        "id": i,
        "name": "check-%d" % i,
        "status": "completed",
        "conclusion": conclusion,
    }


def _fake_github(runs: list[dict], total_count: int):
    """A urlopen that serves the PR detail, its combined status and a paginated check-runs list."""

    def fake_urlopen(req, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        query = urllib.parse.parse_qs(url.query)
        if url.path.endswith("/commits/%s/check-runs" % _SHA):
            per_page = min(int(query.get("per_page", ["30"])[0]), 100)
            page = int(query.get("page", ["1"])[0])
            chunk = runs[(page - 1) * per_page : page * per_page]
            return _JsonResp({"total_count": total_count, "check_runs": chunk})
        if url.path.endswith("/commits/%s/status" % _SHA):
            return _JsonResp({"state": "success", "statuses": [], "total_count": 0})
        if url.path.endswith("/pulls/7"):
            return _JsonResp({"mergeable": True, "mergeable_state": "clean"})
        raise AssertionError("unexpected GitHub read: %s" % req.full_url)

    return fake_urlopen


def test_a_failing_31st_check_run_makes_the_pr_ci_read_failing(monkeypatch):
    runs = [_run(i) for i in range(1, 31)] + [_run(31, "failure")]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_github(runs, total_count=31))

    pr = rn.build_pr("o/r", _OPEN_PR)

    assert pr.ci == "failure"


def test_a_check_runs_read_short_of_total_count_is_not_read_as_green(monkeypatch):
    # GitHub reports 31 runs but serves only 30: the 31st is unseen, so green is unproven.
    runs = [_run(i) for i in range(1, 31)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_github(runs, total_count=31))

    try:
        pr = rn.build_pr("o/r", _OPEN_PR)
    except ValueError:
        return  # a refused read, the way deploy_git.read_check_pages refuses one
    assert pr.ci != "success"
