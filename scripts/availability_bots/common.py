#!/usr/bin/env python3
"""Thin re-export of the shared watcher helpers for the availability-watcher bots.

The generic fetch -> notify -> healthcheck-ping helpers used to live here directly; they
moved to ``scripts/lib/watcher.py`` so a watcher outside this directory (see
``scripts/watchers/``) can use the same building blocks without importing across an
unrelated bot's package. This module re-exports them under their original names so
``glenstone-bot.py`` and ``osteria-francescana-bot.py`` keep working with no code change.

Secrets -- the Discord webhook and the healthcheck ping URL -- are read from the
environment so they never live in the repo. See ``.env.example`` for the variable
names; export them via your shell, a cron ``EnvironmentFile``, or a systemd unit's
``Environment=`` directive.
"""

from __future__ import annotations

# A directly-invoked script gets only its OWN directory on sys.path (repo-root CLAUDE.md,
# "Directory Structure"); reaching `lib.watcher` needs the explicit bootstrap.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import requests

from lib.watcher import (
    DISCORD_USER_AGENT,
    REQUEST_TIMEOUT,
    configure_logging,
    new_session,
    ping_healthcheck,
    require_env,
    send_discord_notification,
)

__all__ = [
    "DISCORD_USER_AGENT",
    "REQUEST_TIMEOUT",
    "configure_logging",
    "new_session",
    "ping_healthcheck",
    "require_env",
    "requests",
    "send_discord_notification",
]
