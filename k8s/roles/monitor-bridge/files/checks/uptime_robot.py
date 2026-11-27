"""The Uptime Robot console against the monitors docs/uptime-robot-monitors.md names.

The monitors were created by hand in the console, and nothing in the repo can set them. A
monitor deleted or paused there leaves no trace in git, so the off-premises backstop two
runbooks cite can vanish unnoticed.

This POSTs v2 `getMonitors` with the account's READ-ONLY key and is down when a monitor in
`UPTIME_ROBOT_MONITORS` is missing or paused. The pod's env renders that list from
monitor-bridge's `monitor_bridge_uptime_robot_monitors`, and a test holds it to the doc's
table (tests/check_bodies/test_check_uptime_robot.py). A monitor that is down is not a failure here:
Uptime Robot pages on that itself.

`bridge.streaks.cached_success` holds the cache: a success is cached for
UPTIME_ROBOT_PROBE_INTERVAL_S (a day), a failure is never cached, so a red row re-probes every
cycle and clears one cycle after the console is fixed. A cycle is 300 s, well inside the free
plan's 10 requests a minute.
"""

from collections.abc import Sequence

from bridge.config import Config
from bridge.sources import Sources
from bridge.streaks import ProbeCache, cached_success
from bridge.types import CheckResult, JsonObject, as_object, as_object_list

# v2's `status`: 0 paused, 1 not checked yet, 2 up, 8 seems down, 9 down.
PAUSED = 0


def fetch_monitors(cfg: Config, src: Sources) -> list[JsonObject]:
    """Every monitor of the account, as v2 `getMonitors` returns them.

    It asks for every monitor rather than filtering by id: how the API answers a filter
    naming a deleted id is undocumented, and a deletion must read as a verdict.

    Raises:
        RuntimeError: the API refused the request or returned a partial page, so the list
          cannot show that a monitor is missing. A transport error raises as `src` does.
    """
    body = as_object(
        src.post_json(
            cfg.UPTIME_ROBOT_API_URL,
            {"api_key": cfg.UPTIME_ROBOT_API_KEY, "format": "json"},
        ),
        "getMonitors response",
    )
    if body.get("stat") != "ok":
        raise RuntimeError("the API refused the monitor list: %s" % body.get("error"))
    monitors = as_object_list(body.get("monitors"), "getMonitors monitors")
    pagination = body.get("pagination")
    total = pagination.get("total") if isinstance(pagination, dict) else None
    if isinstance(total, int) and total > len(monitors):
        raise RuntimeError(
            "the API returned %d of %d monitors" % (len(monitors), total)
        )
    return monitors


def uptime_robot_verdict(
    live: Sequence[JsonObject], expected: Sequence[dict]
) -> tuple[bool, str]:
    """(ok, msg) for the console's monitors against the documented ones. Pure."""
    by_id = {str(m.get("id")): m for m in live}
    problems: list[str] = []
    names: list[str] = []
    for want in expected:
        got = by_id.get(want["id"])
        if got is None:
            problems.append("%s (%s): no such monitor" % (want["name"], want["id"]))
        elif got.get("status") == PAUSED:
            problems.append("%s (%s): paused" % (want["name"], want["id"]))
        else:
            continue
        names.append(want["name"])
    if problems:
        return CheckResult(
            False,
            "docs/uptime-robot-monitors.md names a monitor the Uptime Robot console no "
            "longer runs — %s — restore it at https://dashboard.uptimerobot.com, or change "
            "the doc and monitor_bridge_uptime_robot_monitors together"
            % "; ".join(problems),
            names,
        )
    return True, "%d documented Uptime Robot monitors exist and are not paused" % len(
        expected
    )


def check_uptime_robot_monitors(
    cfg: Config,
    src: Sources,
    now: float | None = None,
    probe: ProbeCache | None = None,
) -> tuple[bool, str]:
    """Throttled read of the Uptime Robot console. (ok, msg).

    `src` and `probe` are the seams: a test hands in a fake `src` and a fresh cache rather than
    patching this module. None reads `src.state.uptime_robot_probe`.
    """
    probe = src.state.uptime_robot_probe if probe is None else probe
    if not cfg.UPTIME_ROBOT_API_KEY or not cfg.UPTIME_ROBOT_MONITORS:
        return True, "Uptime Robot monitor check disabled (no API key or monitor list)"

    def read() -> CheckResult:
        try:
            live = fetch_monitors(cfg, src)
        except Exception as e:
            return CheckResult(
                False,
                "failed to read the Uptime Robot console — unverified: %s" % e,
                ["the monitor list"],
            )
        return CheckResult.of(uptime_robot_verdict(live, cfg.UPTIME_ROBOT_MONITORS))

    return cached_success(probe, cfg.UPTIME_ROBOT_PROBE_INTERVAL_S, now, read)
