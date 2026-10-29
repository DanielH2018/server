"""A CronJob run the weekly reboot can interrupt ages out within the hour after boot.

daniel-box's weekly shutdown kills whichever runs are in flight, and each becomes a failed Job
whose pod reads `Unknown` until `failedJobsHistoryLimit` more runs fail. The 2026-10-25 reboot
left `crowdsec-home-ips-29881895` Failed for 15 hours (#6367). So every rendered CronJob with a
run in flight at the shutdown minute sets `ttlSecondsAfterFinished`, at most `TTL_BUDGET`.
Which CronJobs those are is derived from the `weekly_reboot_*` group vars and each CronJob's
schedule, time zone and `activeDeadlineSeconds`, so a new frequent CronJob reaches this test
without an edit here. A moved reboot fails `test_the_shutdown_is_the_one_the_issue_measured`
first, which is the cue to re-read `KNOWN_IN_WINDOW`.

Run: uv run pytest ansible/tests/k8s/test_reboot_window_cronjobs_age_out.py
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from _k8s_render import host_context, rendered_docs

# The issue's verify-by reads `kubectl get pods -A --field-selector=status.phase=Failed` an
# hour after boot. The TTL clock starts once the Job is marked Failed, after the node is back,
# so 45 minutes leaves the boot and the kubelet's status sync a quarter of an hour.
TTL_BUDGET = 2700
# A CronJob with no `activeDeadlineSeconds` is assumed to run up to this long.
DEFAULT_RUN_SECONDS = 600
# The CronJobs the derivation must find at the reboot schedule this repo ships.
KNOWN_IN_WINDOW = frozenset({"crowdsec-home-ips", "sonarr-air-date-guard"})


def _field_matches(field: str, value: int) -> bool:
    """Whether one cron field (`*`, `*/n`, `a`, `a-b`, `a-b/n`, comma lists) admits `value`."""
    for part in field.split(","):
        base, _, step = part.partition("/")
        if base == "*":
            low, high = 0, 10_000
        elif "-" in base:
            low, high = (int(x) for x in base.split("-"))
        else:
            low = high = int(base)
            if not step:
                if value == low:
                    return True
                continue
        if low <= value <= high and (value - low) % int(step or 1) == 0:
            return True
    return False


def fires_at(schedule: str, moment: datetime) -> bool:
    """Whether the five-field `schedule` fires at `moment`'s minute, in `moment`'s zone."""
    minute, hour, day, month, weekday = schedule.split()
    cron_weekday = moment.isoweekday() % 7
    return (
        _field_matches(minute, moment.minute)
        and _field_matches(hour, moment.hour)
        and _field_matches(day, moment.day)
        and _field_matches(month, moment.month)
        and (
            _field_matches(weekday, cron_weekday)
            or (weekday == "7" and cron_weekday == 0)
        )
    )


def shutdown_time(context: dict) -> datetime:
    """The minute the weekly reboot halts the node, on a reboot day (2026-10-25 for #6367)."""
    zone = ZoneInfo(context["weekly_reboot_timezone"])
    day = datetime(2026, 10, 25, tzinfo=zone)
    day += timedelta(
        days=(int(context["weekly_reboot_weekday"]) - day.isoweekday() % 7) % 7
    )
    return day.replace(
        hour=int(context["weekly_reboot_hour"]),
        minute=int(context["weekly_reboot_minute"]),
    ) + timedelta(minutes=int(context["weekly_reboot_shutdown_delay_minutes"]))


def in_flight_at(cronjob: dict, shutdown: datetime) -> bool:
    """Whether a run of `cronjob` can still be running at `shutdown`."""
    spec = cronjob["spec"]
    zone = ZoneInfo(spec.get("timeZone") or "Etc/UTC")
    run = int(
        spec["jobTemplate"]["spec"].get("activeDeadlineSeconds") or DEFAULT_RUN_SECONDS
    )
    local = shutdown.astimezone(zone)
    return any(
        fires_at(spec["schedule"], local - timedelta(minutes=back))
        for back in range(0, run // 60 + 1)
    )


def unbounded(docs, shutdown: datetime) -> dict[str, object]:
    """The in-window CronJobs whose finished Jobs outlive `TTL_BUDGET`, with their TTL."""
    found = {}
    for doc in docs:
        if doc.get("kind") != "CronJob" or not in_flight_at(doc, shutdown):
            continue
        ttl = doc["spec"]["jobTemplate"]["spec"].get("ttlSecondsAfterFinished")
        if ttl is None or int(ttl) > TTL_BUDGET:
            found[doc["metadata"]["name"]] = ttl
    return found


def _cronjobs() -> list[dict]:
    return [doc for _, _, doc in rendered_docs() if doc.get("kind") == "CronJob"]


def _cronjob(name: str, schedule: str, ttl=None, deadline=240) -> dict:
    spec = {"activeDeadlineSeconds": deadline}
    if ttl is not None:
        spec["ttlSecondsAfterFinished"] = ttl
    return {
        "kind": "CronJob",
        "metadata": {"name": name},
        "spec": {
            "schedule": schedule,
            "timeZone": "Etc/UTC",
            "jobTemplate": {"spec": spec},
        },
    }


SHUTDOWN = datetime(2026, 10, 25, 7, 35, tzinfo=UTC)


def test_the_shutdown_is_the_one_the_issue_measured() -> None:
    assert shutdown_time(host_context()) == SHUTDOWN


def test_the_derivation_finds_the_known_in_window_cronjobs() -> None:
    shutdown = shutdown_time(host_context())
    found = {
        doc["metadata"]["name"] for doc in _cronjobs() if in_flight_at(doc, shutdown)
    }
    assert found >= KNOWN_IN_WINDOW


def test_every_in_window_cronjob_ages_out_inside_the_budget() -> None:
    assert unbounded(_cronjobs(), shutdown_time(host_context())) == {}


def test_an_in_window_cronjob_without_a_ttl_is_flagged() -> None:
    docs = [
        _cronjob("every-five", "*/5 * * * *"),
        _cronjob("long-ttl", "*/5 * * * *", 7200),
    ]
    assert unbounded(docs, SHUTDOWN) == {"every-five": None, "long-ttl": 7200}


def test_an_in_window_cronjob_with_a_short_ttl_and_an_out_of_window_one_are_clean() -> (
    None
):
    docs = [
        _cronjob("every-five", "*/5 * * * *", 1800),
        _cronjob("nightly", "30 4 * * *"),
        # Fires at 07:30 with a 240s deadline, so it has finished by 07:35.
        _cronjob("half-past", "30 * * * *"),
    ]
    assert unbounded(docs, SHUTDOWN) == {}
