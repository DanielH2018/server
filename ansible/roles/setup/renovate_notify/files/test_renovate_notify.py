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


class _Proc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_configured_token_wins_without_running_gh():
    def never(*_a, **_k):
        raise AssertionError("gh must not run when config.env carries a token")

    assert rn.github_token({"GITHUB_TOKEN": "ghp_cfg"}, never) == "ghp_cfg"


def test_gh_auth_token_fills_an_empty_config():
    calls = []

    def run(cmd, **_k):
        calls.append(cmd)
        return _Proc(0, "gho_cli\n")

    assert rn.github_token({}, run) == "gho_cli"
    assert calls == [["gh", "auth", "token"]]


def test_no_token_anywhere_stays_anonymous():
    """The reject half: a logged-out gh or a missing binary must degrade to the anonymous
    request this notifier always made, never crash the daily run into a false OnFailure page."""
    assert rn.github_token({}, lambda *_a, **_k: _Proc(1, "")) == ""

    def boom(*_a, **_k):
        raise FileNotFoundError("gh")

    assert rn.github_token({"GITHUB_TOKEN": "  "}, boom) == ""


def test_discord_returns_false_without_webhook():
    assert rn.discord("", "hi") is False
