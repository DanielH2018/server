"""discord_post's opt-in spool: a post the host could not deliver is sent on a later call (#3905).

daniel-box reached nothing off the host from 2026-10-05 23:55 to about 2026-10-08 21:55 UTC
(#3882), and renovate-agent's in-script post at 11:04 on 2026-10-08 was lost with nothing to
try it again.
"""

import io
import json
import urllib.error
from email.message import Message
from unittest import mock

import host_lib


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Discord:
    """A webhook that answers each POST from a script of outcomes, recording what arrived."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.delivered: list[str] = []

    def urlopen(self, req, timeout=None):
        outcome = self.outcomes.pop(0) if self.outcomes else 204
        if outcome == "down":
            raise urllib.error.URLError("[Errno -2] Name or service not known")
        if outcome >= 400:
            raise urllib.error.HTTPError(
                req.full_url, outcome, "err", Message(), io.BytesIO(b"")
            )
        self.delivered.append(json.loads(req.data)["content"])
        return _Resp(outcome)


def _post(discord, spool, content, **kwargs):
    with mock.patch("host_lib.urllib.request.urlopen", discord.urlopen):
        return host_lib.discord_post(
            "https://x", content, "ua", spool_dir=str(spool), **kwargs
        )


def test_a_post_lost_to_an_outage_is_delivered_on_the_next_call(tmp_path):
    spool = tmp_path / "spool"
    discord = _Discord("down")
    assert _post(discord, spool, "first", marker="renovate:") is False
    assert len(list(spool.glob("*.json"))) == 1

    assert _post(discord, spool, "second") is True
    first, second = discord.delivered
    assert first.startswith("renovate: first\n(delayed: first attempt ")
    assert first.endswith(" UTC)")
    assert second == "second"  # sent at once, so it carries no delay line
    assert not list(spool.glob("*.json"))


def test_queued_posts_flush_oldest_first(tmp_path):
    spool = tmp_path / "spool"
    discord = _Discord("down")
    for n in range(3):
        _post(discord, spool, f"m{n}")
    _post(discord, spool, "now")
    assert [m.split("\n")[0] for m in discord.delivered] == ["m0", "m1", "m2", "now"]


def test_while_the_queue_cannot_flush_a_new_post_joins_it_without_an_attempt(tmp_path):
    spool = tmp_path / "spool"
    discord = _Discord("down", "down")
    _post(discord, spool, "first")
    _post(discord, spool, "second")
    assert (
        discord.outcomes == []
    )  # one attempt per call: the flush, not the new post too
    assert len(list(spool.glob("*.json"))) == 2


def test_a_rejected_post_is_not_queued_and_a_rejected_queued_post_is_dropped(tmp_path):
    spool = tmp_path / "spool"
    discord = _Discord(400)
    _post(discord, spool, "bad payload")
    assert not list(spool.glob("*.json"))

    discord = _Discord(503, 400)
    _post(discord, spool, "queued")
    assert _post(discord, spool, "next") is True
    assert discord.delivered == ["next"]  # the 400 dropped "queued" instead of blocking
    assert not list(spool.glob("*.json"))


def test_a_429_is_queued(tmp_path):
    spool = tmp_path / "spool"
    _post(_Discord(429), spool, "rate limited")
    assert len(list(spool.glob("*.json"))) == 1


def test_one_call_flushes_at_most_the_burst_limit(tmp_path):
    spool = tmp_path / "spool"
    discord = _Discord(*["down"] * 6)
    for n in range(6):
        _post(discord, spool, f"m{n}")
    _post(discord, spool, "now")
    assert len(discord.delivered) == host_lib.DISCORD_SPOOL_FLUSH_MAX + 1
    assert len(list(spool.glob("*.json"))) == 6 - host_lib.DISCORD_SPOOL_FLUSH_MAX


def test_the_spool_keeps_only_the_newest_entries(tmp_path):
    spool = tmp_path / "spool"
    queued = host_lib.DISCORD_SPOOL_MAX + 1
    discord = _Discord(*["down"] * queued)
    for n in range(queued):
        _post(discord, spool, f"m{n}")
    assert len(list(spool.glob("*.json"))) == host_lib.DISCORD_SPOOL_MAX
    _post(discord, spool, "now")
    assert discord.delivered[0].startswith("m1\n")  # m0 was the one dropped


def test_without_a_spool_dir_a_failed_post_is_only_logged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    discord = _Discord("down")
    with mock.patch("host_lib.urllib.request.urlopen", discord.urlopen):
        assert host_lib.discord_post("https://x", "hi", "ua") is False
    assert list(tmp_path.iterdir()) == []


def test_an_empty_webhook_queues_nothing(tmp_path):
    spool = tmp_path / "spool"
    assert host_lib.discord_post("", "hi", "ua", spool_dir=str(spool)) is False
    assert not spool.exists()


def test_a_long_queued_post_keeps_its_delay_line_inside_the_cap(tmp_path):
    spool = tmp_path / "spool"
    discord = _Discord("down")
    _post(discord, spool, "x" * 5000)
    _post(discord, spool, "now")
    delayed = discord.delivered[0]
    assert len(delayed) <= host_lib.DISCORD_MAX
    assert host_lib.DISCORD_TRUNCATED in delayed
    assert delayed.endswith(" UTC)")
