"""How gitops_deploy.py gets an alert out, exercised by calling it.

discord() must route through host_lib.discord_post with its own User-Agent (Discord sits
behind Cloudflare, which 403s the default urllib UA) and the configured webhook. deliver()
must queue BEFORE it posts: alert_once advances its per-SHA marker first, so a process death
inside the 10s POST would otherwise lose the alert for good, and the ff-merged channels never
re-reach their alert code on a later tick. A delivered alert must then LEAVE the queue (the
baseline trap: comparing the removal against the pre-queue dict makes it a permanent no-op
and drain_pending() reposts forever), an undelivered one must stay, and the queue must be
capped through the tested cap_pending() with every drop logged. drain_pending() clears
exactly what it delivered. Every test runs against the canned config and a tmp state dir
from conftest.py; the ordering of drain_pending() inside main() is in
test_gitops_deploy_main_branches.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_alert_delivery.py

import json
import pathlib
import urllib.request

import deploy_health
import pytest

import deploy_alerts
from deploy_toolbox import DeployTools


def _pending(state_dir: pathlib.Path) -> dict[str, str]:
    path = state_dir / "pending_alerts.json"
    return json.loads(path.read_text()) if path.exists() else {}


# ── discord(): the transport contract ─────────────────────────────────────────────────────────
def test_discord_posts_to_the_configured_webhook_with_its_own_user_agent(
    gitops_deploy, monkeypatch, settings
):
    seen: list[urllib.request.Request] = []

    class _Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        assert timeout == 10
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # The REAL discord_post default, so what reaches urlopen is what a host would send.
    assert deploy_alerts.discord(DeployTools(), settings, "hello") is True
    (req,) = seen
    # The webhook comes from the config, not a literal; the UA is the deployer's own.
    assert req.full_url == "https://discord.example/webhook"
    assert req.get_header("User-agent") == "gitops-deploy"
    assert isinstance(req.data, bytes), "the alert POST carried no bytes body"
    assert json.loads(req.data)["content"] == "hello"


def test_discord_reports_a_failed_post_as_undelivered(
    gitops_deploy, monkeypatch, settings
):
    # False, never an exception: the caller queues on False, and alerting must not crash a tick.
    def fake_urlopen(_req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert deploy_alerts.discord(DeployTools(), settings, "hello") is False


# ── deliver(): queue-first, then clear on a confirmed send ────────────────────────────────────
def _sender(state_dir, result) -> tuple[DeployTools, list[dict[str, str]]]:
    """A DeployTools whose webhook records what the queue file held at the moment of the POST.

    `result` is what the post returns, or an exception to raise from it — a death inside
    urlopen. Returns the tools and the record.
    """
    at_send: list[dict[str, str]] = []

    def discord_post(_webhook: str, _content: str) -> bool:
        at_send.append(_pending(state_dir))
        if isinstance(result, BaseException):
            raise result
        return result

    return DeployTools(discord_post=discord_post), at_send


def test_deliver_has_queued_the_alert_by_the_time_it_posts(
    gitops_deploy, monkeypatch, state_dir, settings
):
    tools, at_send = _sender(state_dir, True)
    assert (
        deploy_alerts.deliver(
            tools, gitops_deploy.STATE, settings, "secrets:abc", "rotated"
        )
        is True
    )
    assert at_send == [{"secrets:abc": "rotated"}], (
        "deliver() posts before it persists the queue — a death inside the 10s POST then drops "
        "the alert permanently, because alert_once has already advanced its marker"
    )


def test_a_death_inside_the_post_leaves_the_alert_queued(
    gitops_deploy, monkeypatch, state_dir, settings
):
    # The 2026-08-31 review M-1: a reboot, a `systemctl stop` or the UPS shutdown chain landing
    # inside urlopen. drain_pending() at the top of the next tick reposts what is queued here.
    tools, _at_send = _sender(state_dir, RuntimeError("SIGTERM mid-POST"))
    with pytest.raises(RuntimeError, match="mid-POST"):
        deploy_alerts.deliver(
            tools, gitops_deploy.STATE, settings, "secrets:abc", "rotated"
        )
    assert _pending(state_dir) == {"secrets:abc": "rotated"}


def test_a_delivered_alert_leaves_the_queue(
    gitops_deploy, monkeypatch, state_dir, settings
):
    # Queue-first has one trap: guard the post-send persist against the pre-queue dict and a
    # delivered alert is never removed, so drain_pending() reposts it every tick forever.
    tools, _at_send = _sender(state_dir, True)
    assert (
        deploy_alerts.deliver(
            tools, gitops_deploy.STATE, settings, "secrets:abc", "rotated"
        )
        is True
    )
    assert _pending(state_dir) == {}


def test_an_undelivered_alert_stays_queued(
    gitops_deploy, monkeypatch, state_dir, settings
):
    tools, _at_send = _sender(state_dir, False)
    assert (
        deploy_alerts.deliver(
            tools, gitops_deploy.STATE, settings, "secrets:abc", "rotated"
        )
        is False
    )
    assert _pending(state_dir) == {"secrets:abc": "rotated"}


# ── deliver(): the queue is bounded, and every drop is logged ─────────────────────────────────
# cap_pending() has its own behavioural tests in test_deploy_health.py, but a pure function
# nobody calls is inert — the failure mode this repo has already paid for twice (volume-claim's
# short-circuit fired for 0 of 25 claims behind 16 passing tests). This is the call site.


def test_deliver_caps_the_queue_and_logs_each_drop(
    gitops_deploy, monkeypatch, state_dir, capsys, settings
):
    """Without the cap the queue is unbounded.

    Nothing reads the file back except drain_pending(), so a permanently broken webhook grows it
    every 30 minutes forever. A drop must reach the journal (which Loki indexes) naming the alert
    discarded.
    """
    limit = deploy_health.PENDING_ALERTS_MAX
    full = {f"tasks:{i:040x}": f"alert {i}" for i in range(limit)}
    deploy_alerts.write_pending(gitops_deploy.PENDING_ALERTS_FILE, full)
    tools, _at_send = _sender(state_dir, False)

    deploy_alerts.deliver(
        tools, gitops_deploy.STATE, settings, "secrets:new", "one more, undelivered"
    )

    kept = _pending(state_dir)
    oldest = next(iter(full))
    assert len(kept) == limit
    assert "secrets:new" in kept and oldest not in kept, "oldest first"
    assert f"dropping oldest undelivered {oldest}" in capsys.readouterr().out


def test_cap_pending_is_the_tested_implementation():
    """The deployer must bound the queue with the pure function its tests cover, not a second
    copy that can drift from it."""
    assert deploy_alerts.cap_pending is deploy_health.cap_pending
    assert deploy_health.PENDING_ALERTS_MAX == deploy_health.PENDING_ALERTS_MAX


# ── drain_pending(): resend, and clear only what was confirmed ────────────────────────────────
def test_drain_pending_clears_exactly_what_it_delivered(
    gitops_deploy, monkeypatch, state_dir, settings
):
    deploy_alerts.write_pending(
        gitops_deploy.PENDING_ALERTS_FILE, {"secrets:a": "first", "tasks:b": "second"}
    )
    tools = DeployTools(discord_post=lambda _webhook, content: content == "first")
    deploy_alerts.drain_pending(tools, gitops_deploy.STATE, settings)
    assert _pending(state_dir) == {"tasks:b": "second"}


def test_drain_pending_with_nothing_queued_posts_nothing(
    gitops_deploy, monkeypatch, state_dir, settings
):
    posts: list[str] = []
    tools = DeployTools(
        discord_post=lambda _webhook, content: posts.append(content) or True
    )
    deploy_alerts.drain_pending(tools, gitops_deploy.STATE, settings)
    assert posts == []
    assert not (state_dir / "pending_alerts.json").exists()
