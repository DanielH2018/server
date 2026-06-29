#!/usr/bin/env python3
"""Watch Osteria Francescana (via CoverManager) for a table on the target dates.

For each date in ``TARGET_DATES`` it asks CoverManager for that day's availability and
alerts if a party size is offered at ``WANTED_TIME``, then pings the run healthcheck.

    uv run python scripts/availability_bots/osteria-francescana-bot.py

Required env (see .env.example): OSTERIA_DISCORD_WEBHOOK_URL, OSTERIA_HEALTHCHECK_URL
"""

from __future__ import annotations

import re
from datetime import datetime

import requests

from common import (
    REQUEST_TIMEOUT,
    configure_logging,
    new_session,
    ping_healthcheck,
    require_env,
    send_discord_notification,
)

logger = configure_logging("osteria-francescana-bot")

API_URL = "https://www.covermanager.com/reservation/update_hour_people/0"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": (
        "https://www.covermanager.com/reservation/module_restaurant/"
        "restaurante-osteriafrancescana/english"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

# Dates to watch and the seating time we care about.
TARGET_DATES = [datetime(2025, 6, 29)]
WANTED_TIME = "12:30"

# CoverManager returns HTML fragments; scrape party sizes and offered times out of them.
PEOPLE_RE = re.compile(r"(\d+)\s*people")
TIME_RE = re.compile(r"\d{2}:\d{2}")


def parse_availability(payload: dict, wanted_time: str) -> list[str]:
    """Return the offered party sizes when ``wanted_time`` is among the offered times.

    ``payload['people_box']`` is an HTML fragment like "... 2 people ..." and
    ``payload['hour_box']`` like "... 12:30 ...". Returns the matched party sizes if the
    wanted time is on offer, else an empty list. Pure (no I/O) so it's unit-testable.
    """
    people = PEOPLE_RE.findall(payload.get("people_box", ""))
    times = TIME_RE.findall(payload.get("hour_box", ""))
    return people if people and wanted_time in times else []


def check_date(session: requests.Session, date: datetime) -> list[str]:
    """Query CoverManager for one date and return offered party sizes at ``WANTED_TIME``."""
    form_data = {
        "language": "english",
        "restaurant": "restaurante-osteriafrancescana",
        "dia": date,  # sent as-is (str(datetime)), matching the original request
        "people": 2,
        "only_this_people": "",
        "min_people": "",
        "max_people": 10,
        "time_fix": "",
        "skip_blocked_tables": "false",
        "marketplace": "false",
    }
    response = session.post(API_URL, data=form_data, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return parse_availability(response.json(), WANTED_TIME)


def main() -> None:
    webhook_url = require_env("OSTERIA_DISCORD_WEBHOOK_URL")
    healthcheck_url = require_env("OSTERIA_HEALTHCHECK_URL")
    session = new_session(HEADERS)

    all_ok = True
    for date in TARGET_DATES:
        try:
            people = check_date(session, date)
        except (requests.RequestException, ValueError) as exc:
            logger.error("Availability check failed for %s: %s", date.date(), exc)
            all_ok = False
            continue

        if people:
            logger.info(
                "Table for %s people found on %s at %s.",
                "/".join(people),
                date.date(),
                WANTED_TIME,
            )
            send_discord_notification(
                webhook_url,
                f"Osteria Francescana: tables for {', '.join(people)} people "
                f"on {date:%d-%m-%Y} at {WANTED_TIME}.",
                logger,
            )
        else:
            logger.info("No table at %s on %s.", WANTED_TIME, date.date())

    ping_healthcheck(healthcheck_url, logger, success=all_ok)


if __name__ == "__main__":
    main()
