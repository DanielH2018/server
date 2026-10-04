#!/usr/bin/env python3
"""Decide what the weekly-reboot maintenance window in Uptime Kuma should say.

Runs as the two python stages of the maintenance-sync CronJob, either side of a kuma-cli
read: only kuma-cli can talk to Kuma, and only this script can decide what to say to it.

    --phase status   read Kuma's /metrics, write the ids of the monitors that are DOWN
    --phase select   read `kuma maintenance list`, write the id of our window (or nothing)
    --phase plan     read that window's detail, write the payload to add or edit (or nothing)

Two phases because Kuma's maintenance list does not carry the monitor membership.
`getMaintenanceList` emits `toPublicJSON()` (server/model/maintenance.js, 2.5.5), which has
the schedule and no monitors; only `getMaintenance` fills them in, and it takes the numeric
id the list stage found. Membership is the field that decays — "every monitor" grows on every
deploy that adds a tile — so a reconcile that compared only the list would look correct
forever and cover a shrinking set.

The window is DERIVED from the reboot cron, never written twice. `window.json` carries the
four `weekly_reboot_*` values out of group_vars/all.yml plus this role's lead and recovery
allowance; the cron expression and the duration below are computed from them, so moving the
reboot moves the window.

`--out` is written ONLY when the live window differs from the derived one, and the apply
stage runs kuma-cli only when that file exists. Kuma's editMaintenance rewrites the row and
deletes and reinserts every monitor_maintenance row, in a SQLite database on a Longhorn
volume whose changed blocks ship to B2 nightly — the same reason the status-page sync beside
this one is conditional.

A monitor already DOWN when the window opens is left OUT of it, in the one run before the
window opens (#3506). Kuma's `isImportantForNotification` (server/model/monitor.js, 2.5.5)
counts `MAINTENANCE -> DOWN` as a page, so a DOWN monitor carried through the window pages a
second time when the window closes, for a fault that never changed. Left out, it stays DOWN
throughout and `DOWN -> DOWN` pages nothing. kuma-cli has no command that reads a monitor's
status, so the status phase reads Kuma's Prometheus exporter with `prometheus_kuma_api_key`.
Two gaps remain: a monitor that goes DOWN between that run and the opening still pages twice,
and a stale or empty key keeps every monitor in the window, as it was before #3506.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

# Kuma's exporter labels every series with the monitor's numeric id; 0 is DOWN
# (server/prometheus.js, 2.5.5: "1 = UP, 0= DOWN, 2= PENDING, 3= MAINTENANCE").
_MONITOR_STATUS = re.compile(r"^monitor_status\{(?P<labels>.*)\}\s+(?P<value>\S+)\s*$")
_MONITOR_ID = re.compile(r'(?:^|,)monitor_id="(?P<id>\d+)"')


def window_cron(config: dict) -> tuple[str, int]:
    """The window's cron expression and length in minutes, derived from the reboot cron.

    The window OPENS `lead_minutes` before the reboot cron fires, which is earlier than the
    hosts actually go down — the job's `shutdown -r +N` adds `shutdown_delay_minutes` on top.
    The lead covers a monitor that flips in the seconds around the cron, and the length is
    the sum of everything the window has to span: the lead, that shutdown delay, and the
    recovery allowance for the boot and the rollout that follow.
    """
    fires_at = int(config["reboot_hour"]) * 60 + int(config["reboot_minute"])
    lead = int(config["lead_minutes"])
    opens_at = fires_at - lead
    weekday = int(config["reboot_weekday"])
    if opens_at < 0:
        # The lead crossed midnight backwards, so the window opens on the previous day.
        weekday = (weekday - 1) % 7
    opens_at %= MINUTES_PER_DAY

    duration = (
        lead
        + int(config["shutdown_delay_minutes"])
        + int(config["recovery_allowance_minutes"])
    )
    if duration >= MINUTES_PER_DAY:
        raise SystemExit(
            f"a {duration}-minute window is a day or longer; check the reboot variables"
        )
    return f"{opens_at % 60} {opens_at // 60} * * {weekday}", duration


def monitor_ids(raw: object) -> list[int]:
    """Every live monitor id out of `kuma monitor list`.

    kuma-cli prints a `HashMap<String, Monitor>` keyed by the numeric id as a string; a list
    is read the same way so a future output shape does not silently produce an empty window.
    """
    if isinstance(raw, dict):
        entries = [
            (key, value) for key, value in raw.items() if isinstance(value, dict)
        ]
    elif isinstance(raw, list):
        entries = [(None, value) for value in raw if isinstance(value, dict)]
    else:
        raise SystemExit(
            f"unreadable monitor list: expected object or array, got {type(raw).__name__}"
        )

    ids = []
    for key, monitor in entries:
        raw_id = monitor.get("id", key)
        if raw_id is not None:
            ids.append(int(raw_id))

    if not ids:
        # An empty list is what Kuma answers mid-wipe (#2076) and while AutoKuma is still
        # reconciling. Writing the window anyway would cover nothing, and Kuma would hold
        # that empty membership until the next run — so fail and leave the live window alone.
        raise SystemExit(
            "monitor list carries no ids: Kuma has no monitors (AutoKuma mid-reconcile, or a "
            "wipe like #2076); the maintenance window is left untouched"
        )
    return sorted(set(ids))


def find_window(raw: object, title: str) -> int | None:
    """The numeric id of the maintenance called `title`, or None if Kuma has none."""
    if isinstance(raw, dict):
        entries = [
            (key, value) for key, value in raw.items() if isinstance(value, dict)
        ]
    elif isinstance(raw, list):
        entries = [(None, value) for value in raw if isinstance(value, dict)]
    else:
        raise SystemExit(
            f"unreadable maintenance list: expected object or array, got {type(raw).__name__}"
        )

    matches = []
    for key, maintenance in entries:
        if maintenance.get("title") != title:
            continue
        found = maintenance.get("id", key)
        if found is not None:
            matches.append(int(found))

    if len(matches) > 1:
        # Two windows with one title suppress twice and diverge on every edit. Which to keep
        # is an operator's call, so say so rather than picking one.
        raise SystemExit(
            f"{len(matches)} maintenances are titled {title!r} (ids {sorted(matches)}); "
            "delete the duplicates in Kuma before this can reconcile"
        )
    return matches[0] if matches else None


def read_live(raw: object) -> dict | None:
    """The live window the detail stage read, or None when there is none yet.

    `null` is what that stage writes when the select phase found no window. kuma-cli's
    `collect_or_unwrap` prints ONE result as an object and several as an array, so a
    single-element array is read as the object it holds. Any other shape fails: reading it as
    "no window" would add a SECOND window beside the one Kuma already has.
    """
    if raw is None:
        return None
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        raw = raw[0]
    if not isinstance(raw, dict):
        raise SystemExit(
            f"unreadable maintenance detail: expected an object, got {type(raw).__name__}"
        )
    if raw.get("id") is None:
        raise SystemExit(
            "the live maintenance carries no id; kuma-cli wrote a shape this "
            "cannot edit"
        )
    return raw


def down_monitor_ids(metrics: str) -> list[int]:
    """The ids of the monitors Kuma's exporter reports DOWN, out of its /metrics text.

    A monitor with no `monitor_status` series is not DOWN: the exporter emits one only after a
    heartbeat has landed since Kuma started, and leaving such a monitor in the window is what
    the window did before #3506.
    """
    ids = set()
    for line in metrics.splitlines():
        match = _MONITOR_STATUS.match(line)
        if match is None or match["value"] != "0":
            continue
        found = _MONITOR_ID.search(match["labels"])
        if found is not None:
            ids.add(int(found["id"]))
    return sorted(ids)


def opens_within(config: dict, now: datetime, minutes: int) -> bool:
    """Whether the window next opens within `minutes` after `now`.

    `now` is read as UTC. The window's own zone is a +00:00-all-year stand-in for UTC, because
    kuma-client cannot spell `UTC` (test_the_window_is_declared_on_the_reboot_crons_clock holds
    the offset), and the alpine image this runs in ships no tz database to convert with.
    """
    minute, hour, _day, _month, weekday = window_cron(config)[0].split()
    opens_at = (int(weekday) * 24 + int(hour)) * 60 + int(minute)
    now = now.astimezone(timezone.utc)
    # Python counts Monday as 0; cron counts Sunday as 0.
    now_at = (((now.weekday() + 1) % 7) * 24 + now.hour) * 60 + now.minute
    return 0 < (opens_at - now_at) % MINUTES_PER_WEEK <= minutes


def desired_payload(config: dict, ids: list[int], status_page_id: int | None) -> dict:
    """The maintenance document kuma-cli sends, in the shape kuma-client deserializes.

    Every key is written, including the ones this strategy does not use. kuma-client's
    `MaintenanceSchedule` has no serde default for `dateRange`, and `TimeZoneOption`'s
    hand-written Deserialize calls `missing_field` for each of the three timezone keys, so an
    omitted key is a load error rather than a default. `[null]` is the empty date range
    kuma-client serializes a `None` as, and Kuma's `jsonToBean` indexes `dateRange[0]`, so it
    cannot be a bare null.
    """
    cron, duration = window_cron(config)
    payload = {
        "strategy": "cron",
        "title": config["title"],
        "description": config["description"],
        "active": True,
        "cron": cron,
        "durationMinutes": duration,
        "dateRange": [None],
        "timeRange": None,
        "timezone": config["timezone"],
        "timezoneOption": config["timezone"],
        # Read and discarded by kuma-client's deserializer, which recomputes it from the
        # identifier; present because the deserializer requires the key.
        "timezoneOffset": "+00:00",
        "monitors": [{"id": monitor_id} for monitor_id in ids],
        "statusPages": [] if status_page_id is None else [{"id": status_page_id}],
    }
    return payload


def comparable(maintenance: dict) -> tuple:
    """The part of a maintenance a change should be judged on.

    `id`, `status` and `timezoneOffset` are excluded: Kuma assigns the first two and derives
    the third, so including them would make every run look like a change. Everything the
    window promises — when it opens, how long it lasts, whether it is active, and what it
    covers — is in here.
    """
    return (
        maintenance.get("title"),
        maintenance.get("description"),
        bool(maintenance.get("active")),
        maintenance.get("strategy"),
        maintenance.get("cron"),
        float(maintenance.get("durationMinutes") or 0),
        maintenance.get("timezoneOption") or maintenance.get("timezone"),
        tuple(
            sorted(
                int(monitor["id"])
                for monitor in maintenance.get("monitors") or []
                if monitor.get("id") is not None
            )
        ),
        tuple(
            sorted(
                int(page["id"])
                for page in maintenance.get("statusPages") or []
                if page.get("id") is not None
            )
        ),
    )


def status(args) -> int:
    """Write the DOWN monitors' ids, or `null` when Kuma's exporter cannot be read.

    Never fails the job. `null` makes the plan declare every monitor, which is the window as
    it was before #3506, and a failed job would instead stop adding new monitors to it.
    """
    key = args.api_key_file.read_text().strip() if args.api_key_file.exists() else ""
    if not key:
        print("no Kuma API key; every monitor stays in the window")
        args.out.write_text("null")
        return 0
    request = urllib.request.Request(
        args.metrics_url,
        # Kuma's apiAuth takes an API key as the basic-auth password with an empty username,
        # the same credential Prometheus scrapes this endpoint with.
        headers={
            "Authorization": "Basic " + base64.b64encode(f":{key}".encode()).decode()
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            metrics = response.read().decode()
    except (OSError, ValueError) as err:
        print(
            f"cannot read {args.metrics_url} ({err}); every monitor stays in the window",
            file=sys.stderr,
        )
        args.out.write_text("null")
        return 0
    down = down_monitor_ids(metrics)
    args.out.write_text(json.dumps(down))
    print(f"monitors DOWN now: {down or 'none'}")
    return 0


def select(args) -> int:
    config = json.loads(args.window.read_text())
    live_id = find_window(json.loads(args.maintenances.read_text()), config["title"])
    args.out.write_text("" if live_id is None else str(live_id))
    print(
        f"no maintenance titled {config['title']!r} yet; one will be created"
        if live_id is None
        else f"maintenance {config['title']!r} is id {live_id}"
    )
    return 0


def plan(args) -> int:
    config = json.loads(args.window.read_text())
    ids = monitor_ids(json.loads(args.monitors.read_text()))
    page = json.loads(args.page.read_text())
    status_page_id = page.get("id") if isinstance(page, dict) else None
    live = read_live(json.loads(args.live.read_text()))

    # DECIDED: the DOWN exclusion applies only in the run before the window opens (#3506).
    # Any other run declares every monitor, so a fault that flaps through the week edits the
    # window at most twice: once to leave it out, once after the window to put it back. The
    # gaps it leaves are in the module docstring.
    down = json.loads(args.down.read_text()) if args.down is not None else None
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if down and opens_within(config, now, int(config["down_exclusion_minutes"])):
        kept = [monitor_id for monitor_id in ids if monitor_id not in set(down)]
        if kept:
            print(
                "left out of the window because they are DOWN already: "
                f"{sorted(set(ids) - set(kept))}"
            )
            ids = kept
        else:
            # Every monitor DOWN is Kuma's own network failing, not a fleet of faults. An
            # empty window would let the reboot page for all of them.
            print("every monitor is DOWN; all of them stay in the window")

    desired = desired_payload(config, ids, status_page_id)

    if live is not None and comparable(live) == comparable(desired):
        print(
            f"maintenance window is already declared as derived "
            f"({desired['cron']}, {desired['durationMinutes']} min, {len(ids)} monitors)"
        )
        return 0

    mode = "add"
    if live is not None:
        mode = "edit"
        desired["id"] = live["id"]

    args.out.write_text(json.dumps(desired, indent=2))
    args.mode_out.write_text(mode)
    print(
        f"maintenance window to {mode}: {desired['cron']} for "
        f"{desired['durationMinutes']} min over {len(ids)} monitors -> {args.out}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("status", "select", "plan"), required=True)
    parser.add_argument("--window", type=Path)
    parser.add_argument("--metrics-url")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--down", type=Path)
    parser.add_argument("--now", help="ISO-8601 instant to plan at; tests pin it")
    parser.add_argument("--maintenances", type=Path)
    parser.add_argument("--monitors", type=Path)
    parser.add_argument("--page", type=Path)
    parser.add_argument("--live", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode-out", type=Path)
    args = parser.parse_args(argv)

    if args.phase == "status":
        if args.metrics_url is None or args.api_key_file is None:
            parser.error("--phase status needs --metrics-url and --api-key-file")
        return status(args)

    if args.window is None:
        parser.error(f"--phase {args.phase} needs --window")
    if args.phase == "select":
        if args.maintenances is None:
            parser.error("--phase select needs --maintenances")
        return select(args)

    missing = [
        flag
        for flag, value in (
            ("--monitors", args.monitors),
            ("--page", args.page),
            ("--live", args.live),
            ("--mode-out", args.mode_out),
        )
        if value is None
    ]
    if missing:
        parser.error(f"--phase plan needs {', '.join(missing)}")
    return plan(args)


if __name__ == "__main__":
    sys.exit(main())
