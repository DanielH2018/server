"""Tests for scripts/lib/watcher.py's generic fetch -> check -> notify loop."""

from __future__ import annotations

import logging

import pytest

from lib.watcher import Watcher, load_state, run_watcher, save_state


@pytest.fixture
def logger() -> logging.Logger:
    log = logging.getLogger("test-watcher")
    log.addHandler(logging.NullHandler())
    return log


# The transition-rule red-proof pair: unchanged state never notifies, a real change always does.


def test_run_watcher_does_not_notify_on_unchanged_state(
    tmp_path, monkeypatch, logger
) -> None:
    sent = []
    monkeypatch.setattr(
        "lib.watcher.send_discord_notification", lambda *a, **k: sent.append(a)
    )
    save_state(tmp_path / "state.json", {"count": 1})

    w = Watcher(
        name="test",
        state_path=tmp_path / "state.json",
        fetch=lambda: {"count": 1},
        check=lambda prev, cur: None if prev == cur else "changed",
        logger=logger,
        webhook_url="https://discord.example/webhook",
    )
    assert run_watcher(w) == 0
    assert sent == []


def test_run_watcher_notifies_on_a_real_transition(
    tmp_path, monkeypatch, logger
) -> None:
    sent = []
    monkeypatch.setattr(
        "lib.watcher.send_discord_notification", lambda *a, **k: sent.append(a)
    )
    save_state(tmp_path / "state.json", {"count": 1})

    w = Watcher(
        name="test",
        state_path=tmp_path / "state.json",
        fetch=lambda: {"count": 2},
        check=lambda prev, cur: None if prev == cur else "changed",
        logger=logger,
        webhook_url="https://discord.example/webhook",
    )
    assert run_watcher(w) == 0
    assert len(sent) == 1
    assert "changed" in sent[0][1]


def test_run_watcher_persists_state_after_a_run(tmp_path, monkeypatch, logger) -> None:
    monkeypatch.setattr("lib.watcher.send_discord_notification", lambda *a, **k: None)
    w = Watcher(
        name="test",
        state_path=tmp_path / "state.json",
        fetch=lambda: {"count": 3},
        check=lambda prev, cur: None,
        logger=logger,
    )
    run_watcher(w)
    assert load_state(tmp_path / "state.json") == {"count": 3}


def test_run_watcher_pings_the_healthcheck_fail_endpoint_when_fetch_raises(
    tmp_path, monkeypatch, logger
) -> None:
    pings = []
    monkeypatch.setattr(
        "lib.watcher.ping_healthcheck",
        lambda url, log, *, success=True: pings.append(success),
    )

    def broken_fetch():
        raise RuntimeError("source unreachable")

    w = Watcher(
        name="test",
        state_path=tmp_path / "state.json",
        fetch=broken_fetch,
        check=lambda prev, cur: None,
        logger=logger,
        healthcheck_url="https://healthchecks.example/ping/abc",
    )
    assert run_watcher(w) == 1
    assert pings == [False]


def test_run_watcher_notifies_the_webhook_when_fetch_raises(
    tmp_path, monkeypatch, logger
) -> None:
    # A fetch failure is a transport problem, not a finding -- it must reach the webhook
    # on every failing run, not just the transition into failure, because a watcher's cron
    # output goes to a loopback-only mailer nobody reads.
    sent = []
    monkeypatch.setattr(
        "lib.watcher.send_discord_notification", lambda *a, **k: sent.append(a)
    )

    def broken_fetch():
        raise RuntimeError("source unreachable")

    w = Watcher(
        name="test",
        state_path=tmp_path / "state.json",
        fetch=broken_fetch,
        check=lambda prev, cur: None,
        logger=logger,
        webhook_url="https://discord.example/webhook",
    )
    assert run_watcher(w) == 1
    assert len(sent) == 1
    assert "run failed" in sent[0][1]


# load_state / save_state


def test_load_state_returns_none_for_a_missing_file(tmp_path) -> None:
    assert load_state(tmp_path / "nope.json") is None


def test_save_state_then_load_state_round_trips(tmp_path) -> None:
    path = tmp_path / "nested" / "state.json"
    save_state(path, {"a": [1, 2, 3]})
    assert load_state(path) == {"a": [1, 2, 3]}
