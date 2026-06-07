#!/usr/bin/env python3
"""Shared helpers for the availability-watcher bots in this folder.

Each bot polls a venue's API for an open slot (museum timed-entry tickets, a
restaurant table, ...) and, when something opens up, posts to a Discord webhook and
pings a healthchecks.io-style monitor. The per-bot specifics (URLs, request shape,
response parsing) live in the bot; everything generic lives here.

Secrets — the Discord webhook and the healthcheck ping URL — are read from the
environment so they never live in the repo. See ``.env.example`` for the variable
names; export them via your shell, a cron ``EnvironmentFile``, or a systemd unit's
``Environment=`` directive.
"""
from __future__ import annotations

import logging
import os

import requests

# Bounded timeout on every network call so an unresponsive venue API can't wedge a
# bot indefinitely (the originals passed no timeout — a hung host would block forever).
REQUEST_TIMEOUT = 30  # seconds


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

    Fail-fast beats a confusing ``None`` flowing into a request URL later — a
    misconfigured bot should die loudly (and not ping its healthcheck) so you notice.
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

    Never raises: a failed notification is logged, not fatal — by the time we get here
    the caller has already found availability and we don't want to crash before the
    healthcheck ping.
    """
    try:
        response = requests.post(
            webhook_url, json={"content": message}, timeout=REQUEST_TIMEOUT
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
