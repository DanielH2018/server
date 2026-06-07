#!/usr/bin/env python3
"""Watch Glenstone's timed-entry calendar and alert when a target date opens up.

Polls the public events calendar API for the dates in ``TARGET_DATES`` and, if any is
no longer ``sold_out``, posts to Discord and pings the run healthcheck.

    uv run python scripts/availability_bots/glenstone-bot.py

Required env (see .env.example): GLENSTONE_DISCORD_WEBHOOK_URL, GLENSTONE_HEALTHCHECK_URL
"""
from __future__ import annotations

import requests

from common import (
    REQUEST_TIMEOUT,
    configure_logging,
    new_session,
    ping_healthcheck,
    require_env,
    send_discord_notification,
)

logger = configure_logging("glenstone-bot")

API_URL = (
    "https://visit.glenstone.org/api/events/"
    "018d18c1-2d4c-1c74-0499-34b5bb488892/calendar?_format=extended"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Dates to watch, as YYYY-MM-DD strings. Edit to taste.
TARGET_DATES = ["2025-08-09"]


def find_available_dates(session: requests.Session) -> list[str]:
    """Return the watched dates that currently show availability (not ``sold_out``)."""
    response = session.get(API_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    entries = response.json().get("calendar", {}).get("_data", [])

    available: list[str] = []
    for entry in entries:
        date = entry.get("date")
        if date not in TARGET_DATES:
            continue
        status = entry.get("status")
        if status and status != "sold_out":
            logger.info("Date %s is available (status=%s).", date, status)
            available.append(date)
        else:
            logger.info("Date %s is sold out.", date)
    return available


def main() -> None:
    webhook_url = require_env("GLENSTONE_DISCORD_WEBHOOK_URL")
    healthcheck_url = require_env("GLENSTONE_HEALTHCHECK_URL")
    session = new_session(HEADERS)

    try:
        available = find_available_dates(session)
    except (requests.RequestException, ValueError) as exc:
        # Network error, non-2xx, or malformed/unexpected JSON: signal the monitor.
        logger.error("Availability check failed: %s", exc)
        ping_healthcheck(healthcheck_url, logger, success=False)
        return

    if available:
        logger.info("Spots available for Glenstone on: %s", ", ".join(available))
        send_discord_notification(
            webhook_url,
            f"Glenstone availability found for: {', '.join(available)}.",
            logger,
        )
    else:
        logger.info("No spots available for Glenstone.")

    ping_healthcheck(healthcheck_url, logger)


if __name__ == "__main__":
    main()
