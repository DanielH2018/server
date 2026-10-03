import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
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
