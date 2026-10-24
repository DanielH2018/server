"""The k3s restart window overlaps no schedule that reads the cluster, derived from the schedules.

H6 (*Verification*, *Window*). The window runs from its `OnCalendar` start to the start plus the
timer's `AccuracySec` plus the service's `TimeoutStartSec`. Every other fire is widened by its own
accuracy. The sources are: each setup role's crons and timers (the sources
`scripts/docs/gen_role_glance.py` reads), each timer a host package ships, and every CronJob and
Longhorn RecurringJob schedule under `ansible/roles/k8s/`. A monotonic timer has no calendar
instant, so it passes only through `EXEMPT`, and each entry names what replaces the check.

Hosts: only daniel-box ships `homelab-k3s-restart.timer` so far. daniel-server's window joins this
test with the PR that adds the component to its package.
"""

import functools
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from _k3s_schedule import Unparseable, calendar_fires, clock_text, cron_fires

from docs.catalog_lib import glance_facts
from lib.estate import Estate
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib import host as host_mode

DEFAULT_ACCURACY_S = 60  # systemd's AccuracySec when a timer sets none
# Mon 2 to Fri 6 November 2026: the weekdays the window runs on, none of them the 1st.
DAYS = [date(2026, 11, 2) + timedelta(days=n) for n in range(5)]
WINDOW_TIMER = "homelab-k3s-restart.timer"

# Each entry names the exclusion that replaces the schedule check.
EXEMPT = {
    "gitops-deploy.timer": "the `all` deploy lock (step 5); H7.19a removes both",
    "homelab-updater.timer": "the `windowed` mark: reconcile never restarts k3s",
    "claude-cgroup-metrics.timer": "writes Claude cgroup metrics; never touches k3s",
    "claude-clone-sync.timer": "syncs a git clone; never touches k3s",
    "claude-dotfiles-sync.timer": "syncs the dotfiles clone; never touches k3s",
    "claude-memory-sync.timer": "copies the memory store to daniel-server; never touches k3s",
    "claude-fanout-server-lander.timer": "lands a merged PR over ssh; reads no cluster state",
    "claude-triage-poll.timer": "polls monitors and starts a session only on an alert episode",
    "homelab-builder.timer": "builds and signs packages; a build during a restart only waits",
    "Sync peer Claude artifacts": "an ssh pull from daniel-server; reads no cluster state",
    # A */5 schedule fires inside any window longer than 5 minutes, so the span cannot avoid it.
    "CronJob crowdsec */5 * * * *": "home-IP lookup; a missed run is not made up (backoffLimit 0)",
}
# Non-vacuity: each scan must find its member, or an empty scan would pass.
CRON_MEMBER = "Release staleness drift check"
CRONJOB_MEMBER = "configarr"
RECURRING_MEMBER = "daily-backup"


@dataclass(frozen=True)
class Schedule:
    name: str
    fires: dict[date, list[int]]
    width: int


def _spread(fire) -> dict[date, list[int]]:
    return {day: fire(day) for day in DAYS}


def calendar_schedule(
    name: str, expr: str, width: int = DEFAULT_ACCURACY_S
) -> Schedule:
    return Schedule(name, _spread(lambda d: calendar_fires(expr, d)), width)


def cron_schedule(name: str, expr: str, tz: str = "UTC", width: int = 0) -> Schedule:
    def fire(day: date) -> list[int]:
        zone = ZoneInfo(tz)
        out = []
        for seconds in cron_fires(expr, day):
            local = datetime.combine(day, time(), zone) + timedelta(seconds=seconds)
            utc = local.astimezone(ZoneInfo("UTC"))
            if utc.date() == day:
                out.append(utc.hour * 3600 + utc.minute * 60 + utc.second)
        return out

    return Schedule(name, _spread(fire), width)


def violations(window: Schedule, end_offset: int, others: list[Schedule]) -> list[str]:
    found = []
    for day in DAYS:
        for start in window.fires[day]:
            for other in others:
                if other.name in EXEMPT:
                    continue
                for fire in other.fires[day]:
                    if fire <= start + end_offset and fire + other.width >= start:
                        found.append(
                            f"{other.name} fires {clock_text(fire)} on {day}, inside the "
                            f"window {clock_text(start)}+{end_offset}s"
                        )
    return found


# ── the sources ─────────────────────────────────────────────────────────────────────────────


def _timer_keys(text: str) -> list[tuple[str, str]]:
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(
            r"^(OnCalendar|OnBootSec|OnUnitActiveSec|OnActiveSec|AccuracySec)=(.*)$",
            text,
            re.M,
        )
    ]


def _timer_schedule(name: str, keys: list[tuple[str, str]], monotonic: list[str]):
    calendars = [v for k, v in keys if k == "OnCalendar" and v]
    accuracy = [v for k, v in keys if k == "AccuracySec" and v]
    width = DEFAULT_ACCURACY_S
    if accuracy:
        width = int(accuracy[-1].removesuffix("s"))
    if not calendars:
        monotonic.append(name)
        return None
    return calendar_schedule(name, calendars[-1], width)


def role_schedules(monotonic: list[str]) -> list[Schedule]:
    found = []
    for role in glance_facts.setup_role_dirs():
        for name, expr in glance_facts.cron_jobs(role):
            found.append(cron_schedule(name, expr))
        for unit, keys in glance_facts.timer_units(role):
            pairs = [(k.split("=", 1)[0], k.split("=", 1)[1]) for k in keys]
            schedule = _timer_schedule(unit, pairs, monotonic)
            if schedule:
                found.append(schedule)
    return found


def package_schedules(package, monotonic: list[str]) -> list[Schedule]:
    texts: dict[str, str] = {}
    for dst, (body, _mode) in package.files.items():
        match = re.fullmatch(r"/usr/lib/systemd/system/([^/]+\.timer)(\.d/[^/]+)?", dst)
        if match:
            texts[match.group(1)] = texts.get(match.group(1), "") + body.decode() + "\n"
    found = []
    for name, text in sorted(texts.items()):
        if name != WINDOW_TIMER:
            schedule = _timer_schedule(name, _timer_keys(text), monotonic)
            if schedule:
                found.append(schedule)
    return found


def _resolve(text: str, variables: dict, extra: dict) -> str:
    merged = {**variables, **extra}
    return re.sub(
        r"\{\{\s*(\w+)\s*\}\}",
        lambda m: str(merged[m.group(1)]) if m.group(1) in merged else m.group(0),
        text,
    )


@functools.cache
def job_schedules() -> tuple[list[Schedule], set[str], set[str]]:
    """CronJob schedules and Longhorn RecurringJob crons, with the role or job names found."""
    estate, found, cronjobs, recurring = Estate(), [], set(), set()
    for role in sorted(K8S_ROLES.iterdir()):
        variables = estate.role_vars(role) if (role / "templates").is_dir() else {}
        for template in sorted((role / "templates").glob("*.j2")):
            text = template.read_text()
            zone = re.search(r"^\s*timeZone:\s*\"?([^\"\n]+)", text, re.M)
            tz = _resolve(zone.group(1).strip(), variables, {}) if zone else "UTC"
            for line in re.finditer(
                r"^\s*(schedule|cron):\s*\"?([^\"\n]+)\"?\s*$", text, re.M
            ):
                kind = "CronJob" if line.group(1) == "schedule" else "RecurringJob"
                for shard in (
                    ("0", "1", "2", "3", "4", "5", "6")
                    if "{{ shard }}" in line.group(2)
                    else ("",)
                ):
                    expr = _resolve(line.group(2), variables, {"shard": shard})
                    if "{{" in expr:
                        raise Unparseable(f"{template}: unresolved {expr!r}")
                    name = f"{kind} {role.name} {expr}"
                    found.append(
                        cron_schedule(name, expr, tz=tz, width=DEFAULT_ACCURACY_S)
                    )
                    (cronjobs if kind == "CronJob" else recurring).add(role.name)
    recurring_names = set(
        re.findall(
            r"name: (daily-backup)",
            (
                K8S_ROLES / "longhorn-backup/templates/longhorn-recurringjob.yaml.j2"
            ).read_text(),
        )
    )
    return found, cronjobs, recurring_names


@pytest.fixture(scope="module")
def box_package(tmp_path_factory):
    root = tmp_path_factory.mktemp("box")
    secrets = root / "secrets.yml"
    secrets.write_text(
        "k3s_readonly_token: x\nups_secondary_push_token: x\nnut_monitor_password: x\n"
    )
    sops = root / "sops"
    sops.mkdir()
    (sops / "daniel-box.sops.yaml").write_text(
        "k3s_readonly_token: x\nups_secondary_push_token: x\nnut_monitor_password: x\n"
    )
    return host_mode.render_host("daniel-box", secrets=secrets, sops_dir=sops)


def _window(package) -> tuple[Schedule, int]:
    timer = package.files[f"/usr/lib/systemd/system/{WINDOW_TIMER}"][0].decode()
    service = package.files["/usr/lib/systemd/system/homelab-k3s-restart.service"][
        0
    ].decode()
    keys = dict(_timer_keys(timer))
    accuracy = int(keys["AccuracySec"].removesuffix("s"))
    found = re.search(r"^TimeoutStartSec=(\d+)min", service, re.M)
    assert found is not None
    timeout = int(found.group(1)) * 60
    return calendar_schedule(
        WINDOW_TIMER, keys["OnCalendar"], accuracy
    ), accuracy + timeout


# ── the derivation ──────────────────────────────────────────────────────────────────────────


def test_the_window_is_found_in_the_box_package_and_runs_on_weekdays_only(box_package):
    window, span = _window(box_package)
    assert all(window.fires[day] == [11 * 3600 + 32 * 60] for day in DAYS)
    assert span == 1 + 7 * 60
    assert (
        calendar_fires(
            yaml.safe_load("v: Mon..Fri *-*-02..31 11:32:00")["v"], date(2026, 11, 1)
        )
        == []
    )


def test_no_schedule_that_reads_the_cluster_fires_inside_the_box_window(box_package):
    window, span = _window(box_package)
    monotonic: list[str] = []
    others = role_schedules(monotonic) + package_schedules(box_package, monotonic)
    others += package_schedules(host_mode.render_package("homelab-updater"), monotonic)
    jobs, _, _ = job_schedules()
    assert violations(window, span, others + jobs) == []
    unlisted = sorted(set(monotonic) - set(EXEMPT))
    assert unlisted == [], f"monotonic timers with no exclusion named: {unlisted}"


def test_each_scan_finds_the_member_that_proves_it_reads_something(box_package):
    names = {s.name for s in role_schedules([])}
    assert any(CRON_MEMBER in n for n in names) or CRON_MEMBER in {
        n for r in glance_facts.setup_role_dirs() for n, _ in glance_facts.cron_jobs(r)
    }
    _, cronjobs, recurring = job_schedules()
    assert CRONJOB_MEMBER in cronjobs
    assert RECURRING_MEMBER in recurring
    assert "gitops-deploy.timer" in {
        u
        for r in glance_facts.setup_role_dirs()
        for u, _ in glance_facts.timer_units(r)
    }


def test_every_exemption_names_a_schedule_that_exists():
    jobs, _, _ = job_schedules()
    crons = {s.name for s in jobs} | {
        n for r in glance_facts.setup_role_dirs() for n, _ in glance_facts.cron_jobs(r)
    }
    timers = {
        u
        for r in glance_facts.setup_role_dirs()
        for u, _ in glance_facts.timer_units(r)
    }
    timers |= {"homelab-updater.timer"}
    missing = sorted(set(EXEMPT) - crons - timers)
    assert missing == [], f"EXEMPT names that no schedule carries: {missing}"


# ── red fixtures: each differs from the accepting window in one place ───────────────────────


def _box_window(accuracy: int = 1) -> tuple[Schedule, int]:
    window = calendar_schedule(WINDOW_TIMER, "Mon..Fri *-*-02..31 11:32:00", accuracy)
    return window, accuracy + 7 * 60


def test_a_host_cron_every_five_minutes_fails():
    window, span = _box_window()
    assert violations(window, span, [cron_schedule("fixture", "*/5 * * * *")])


def test_a_cron_every_ten_minutes_passes():
    window, span = _box_window()
    assert violations(window, span, [cron_schedule("fixture", "*/10 * * * *")]) == []


def test_a_server_window_at_1136_fails_against_the_half_hourly_cron_family():
    window = calendar_schedule(WINDOW_TIMER, "Mon..Fri *-*-02..31 11:36:00", 1)
    assert violations(window, 1 + 7 * 60, [cron_schedule("fixture", "40 11 * * *")])


def test_a_cronjob_at_1155_fails_the_server_window_at_1152():
    window = calendar_schedule(WINDOW_TIMER, "Mon..Fri *-*-02..31 11:52:00", 1)
    other = cron_schedule("fixture CronJob", "55 11 * * *", width=DEFAULT_ACCURACY_S)
    assert violations(window, 1 + 7 * 60, [other])


def test_a_restart_timer_without_accuracysec_carries_1132_to_1140():
    window, span = _box_window(accuracy=DEFAULT_ACCURACY_S)
    assert violations(window, span, [cron_schedule("fixture", "40 11 * * *")])
    window, span = _box_window(accuracy=1)
    assert violations(window, span, [cron_schedule("fixture", "40 11 * * *")]) == []


def test_an_unlisted_monotonic_timer_is_reported():
    monotonic: list[str] = []
    _timer_schedule("fixture.timer", [("OnUnitActiveSec", "10min")], monotonic)
    assert monotonic == ["fixture.timer"]
    assert "fixture.timer" not in EXEMPT


def test_the_monthly_first_is_outside_the_window():
    # The hypervisor drill starts at 11:20 on the 1st; the window skips that day.
    first = date(2026, 11, 1)
    assert cron_fires("20 11 1 * *", first) == [11 * 3600 + 20 * 60]
    assert calendar_fires("Mon..Fri *-*-02..31 11:32:00", first) == []
