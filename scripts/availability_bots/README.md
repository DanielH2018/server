# availability_bots

Small polling bots that watch a venue's API for an open slot and alert when one
appears. Each run: **fetch → check availability → notify Discord → ping healthcheck**.

| Bot | Watches |
| --- | --- |
| `glenstone-bot.py` | Glenstone museum timed-entry calendar (dates in `TARGET_DATES`) |
| `osteria-francescana-bot.py` | Osteria Francescana table via CoverManager (`TARGET_DATES` / `WANTED_TIME`) |

Shared logging, Discord notification, healthcheck ping, and HTTP-session setup live in
`common.py` so the bots stay focused on their venue-specific fetch + parse logic.

## Configuration

What to watch is edited in code (the `TARGET_DATES` / `WANTED_TIME` constants at the top
of each bot). Secrets are read from the environment — never hardcoded:

| Variable | Used by |
| --- | --- |
| `GLENSTONE_DISCORD_WEBHOOK_URL`, `GLENSTONE_HEALTHCHECK_URL` | `glenstone-bot.py` |
| `OSTERIA_DISCORD_WEBHOOK_URL`, `OSTERIA_HEALTHCHECK_URL` | `osteria-francescana-bot.py` |
| `LOG_LEVEL` (optional, default `INFO`) | all bots |

Copy `.env.example` to `.env` and fill it in, or export the variables some other way. A
bot exits immediately if one of its required variables is unset.

## Running

```bash
# Loads the env file, then runs in the repo's pinned env (requests is in the dev group).
set -a; source scripts/availability_bots/.env; set +a
uv run python scripts/availability_bots/glenstone-bot.py
```

The healthcheck is pinged on every completed run, and on the monitor's `/fail` endpoint
if the availability check errors out — so a broken bot alerts instead of silently going
stale. Wire each bot up on a schedule (cron / systemd timer) for continuous watching.
