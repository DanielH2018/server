#!/usr/bin/env python3
"""Generic scaffold for an external-watcher: fetch -> check -> notify -> healthcheck ping.

A watcher polls something outside this repo (a venue's API, a TLS leaf cert, ...) and, when
the thing it watches CHANGES STATE, posts to a Discord webhook -- then pings a
healthchecks.io-style monitor so a broken watcher alerts instead of silently going stale.

This module carries two layers:

  - The low-level helpers (``configure_logging``, ``require_env``, ``new_session``,
    ``send_discord_notification``, ``ping_healthcheck``) were moved here unchanged from
    ``scripts/availability_bots/common.py``, which now re-exports them so the availability
    bots keep working without a code change. Any watcher can use them directly.
  - ``Watcher`` + ``run_watcher`` are new: a generic fetch -> check(previous, current) ->
    notify-on-transition -> healthcheck-ping loop, with state persisted as JSON between runs.
    The availability bots do NOT use this loop -- they notify on every run they find
    availability, which is a deliberate difference (an open slot is worth repeating, a
    transition is not the model there). A watcher that should notify only when its state
    CHANGES (a cert crossing an expiry threshold, a service coming back up, ...) is the
    intended caller of ``run_watcher``.

Secrets (a Discord webhook, a healthcheck ping URL) are read from the environment by the
caller, exactly as the availability bots already do -- never hardcoded here.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

# Bounded timeout on every network call so an unresponsive source can't wedge a watcher
# indefinitely.
REQUEST_TIMEOUT = 30  # seconds

# Explicit UA on the Discord POST. requests' default `python-requests/*` already works, but
# Cloudflare (Discord's edge) has 1010-blocked header-light clients before, so send a real
# UA as defense-in-depth and to keep every Discord POST in this repo uniformly UA'd.
DISCORD_USER_AGENT = "homelab-availability-bot/1.0"


def configure_logging(name: str) -> logging.Logger:
    """Configure console logging once and return a named logger.

    Level comes from ``$LOG_LEVEL`` (default ``INFO``) so you can flip to ``DEBUG``
    from the environment without editing code.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger(name)


def require_env(name: str) -> str:
    """Return a required environment variable's value, or exit with a clear message.

    Fail-fast beats a confusing ``None`` flowing into a request URL later -- a
    misconfigured watcher should die loudly (and not ping its healthcheck) so you notice.
    """
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required environment variable {name!r}. "
            "See scripts/availability_bots/.env.example for the full list."
        )
    return value


def new_session(headers: dict[str, str] | None = None) -> requests.Session:
    """Return a ``requests.Session`` (connection reuse) with optional default headers."""
    session = requests.Session()
    if headers:
        session.headers.update(headers)
    return session


def send_discord_notification(
    webhook_url: str, message: str, logger: logging.Logger
) -> None:
    """Post a plain-text message to a Discord webhook.

    Never raises: a failed notification is logged, not fatal -- by the time we get here
    the caller has already found something worth reporting and we don't want to crash
    before the healthcheck ping.
    """
    try:
        response = requests.post(
            webhook_url,
            json={"content": message},
            headers={"User-Agent": DISCORD_USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()  # Discord replies 204 No Content on success
        logger.info("Discord notification sent.")
    except requests.RequestException as exc:
        logger.error("Failed to send Discord notification: %s", exc)


def ping_healthcheck(
    ping_url: str, logger: logging.Logger, *, success: bool = True
) -> None:
    """Ping a healthchecks.io-style monitor (best-effort).

    Pass ``success=False`` to hit the monitor's ``/fail`` endpoint so a broken run
    actually alerts instead of the monitor silently staying green.
    """
    url = ping_url if success else ping_url.rstrip("/") + "/fail"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info("Healthcheck ping (%s) sent.", "ok" if success else "fail")
    except requests.RequestException as exc:
        logger.warning("Healthcheck ping failed: %s", exc)


# ---------------------------------------------------------------------------------------
# The generic watcher loop.
# ---------------------------------------------------------------------------------------


def load_state(path: Path) -> Any | None:
    """The previous run's persisted state, or ``None`` for a missing/unreadable/first run."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return None


def save_state(path: Path, state: Any) -> None:
    """Persist this run's state as JSON, creating the state directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


@dataclass
class Watcher:
    """One watcher's wiring: what to fetch, how to judge a change, and where to report it.

    Attributes:
        name: Short identifier, used in log lines and the Discord message prefix.
        state_path: Where this watcher's JSON state persists between runs.
        fetch: Returns this run's current state. May raise -- ``run_watcher`` treats a
            raised exception as a failed run (logs it, pings the healthcheck's `/fail`).
        check: ``(previous, current) -> finding message, or None if nothing changed``.
            Called every run, whether or not ``previous`` exists (a first run has
            ``previous=None``).
        logger: Where this watcher logs.
        webhook_url: Discord webhook to notify on a finding. ``None`` disables notification
            (the finding is still logged) -- useful for a watcher with no channel wired yet.
        healthcheck_url: healthchecks.io-style ping URL. ``None`` disables the deadman ping.
    """

    name: str
    state_path: Path
    fetch: Callable[[], Any]
    check: Callable[[Any | None, Any], str | None]
    logger: logging.Logger
    webhook_url: str | None = None
    healthcheck_url: str | None = None


def run_watcher(watcher: Watcher) -> int:
    """Run one fetch -> check -> notify-on-transition -> healthcheck-ping cycle.

    Notifies only when ``watcher.check`` returns a finding -- i.e. on a STATE CHANGE, not
    on every run. State is saved after a successful fetch regardless of whether a finding
    fired, so an unchanged run still advances what "previous" means next time.

    A ``fetch`` failure is a different kind of event and is reported on every run it
    happens, not just the transition into it -- the state-change rule above governs
    findings, not transport failures. It always reaches the webhook (when one is
    configured) rather than only the healthcheck ping: a watcher's cron output goes to a
    loopback-only local mailer nobody reads, so the healthcheck ping alone would make a
    persistent outage invisible on the one channel someone actually watches.

    Returns 0 for a completed run (finding or not) and 1 if ``fetch`` raised.
    """
    previous = load_state(watcher.state_path)

    try:
        current = watcher.fetch()
    except Exception as exc:
        watcher.logger.error("%s: fetch failed: %s", watcher.name, exc)
        if watcher.webhook_url:
            send_discord_notification(
                watcher.webhook_url,
                f"[{watcher.name}] run failed: {exc}",
                watcher.logger,
            )
        if watcher.healthcheck_url:
            ping_healthcheck(watcher.healthcheck_url, watcher.logger, success=False)
        return 1

    finding = watcher.check(previous, current)
    if finding:
        watcher.logger.info("%s: %s", watcher.name, finding)
        if watcher.webhook_url:
            send_discord_notification(
                watcher.webhook_url, f"[{watcher.name}] {finding}", watcher.logger
            )
        else:
            watcher.logger.warning(
                "%s: no webhook configured, finding not sent: %s", watcher.name, finding
            )
    else:
        watcher.logger.info("%s: no state change.", watcher.name)

    save_state(watcher.state_path, current)
    if watcher.healthcheck_url:
        ping_healthcheck(watcher.healthcheck_url, watcher.logger)
    return 0
