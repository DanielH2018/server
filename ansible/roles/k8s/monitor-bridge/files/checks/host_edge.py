"""Edge checks for monitor-bridge: speedtest-tracker's newest result row.

Reads config as
`cfg.X` and its streak counters as `src.state` (`bridge.streaks.State`). Every HTTP query goes
through the `src` argument (`bridge.sources.Sources`), which a test replaces with a `FakeSources`.
The verdict it from-imports from verdicts.host is patched on THIS module, where it is bound. Rule
and enforcement: bridge/config.py's header.

The Pi's resource pressure and published-port arm are rules, not checks: the `hosts` rules
(`PiLoadHigh`, `PiMemoryLow`, `PiDiskFull`, `PiFilesystemReadOnly`, `PiMetricsMissing`) and
`PiPortDead` page them through Alertmanager.
"""

from bridge.config import Config
from bridge.sources import Sources
import bridge.streaks
from bridge.types import as_object, as_object_list
from verdicts.host import speedtest_verdict


def check_speedtest(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Judge speedtest-tracker's newest result row (the SPEEDTEST_* env block in bridge/config_host.py).

    Empty URL/token -> disabled (stays up), like check_ha_heartbeat.

    NO CONSECUTIVE-CYCLE HYSTERESIS ON THE VERDICT, deliberately, and that is still true after
    #2785. The app runs every 6h and this loop every 5 min, so a cycle streak would re-read the
    IDENTICAL row up to 72 times: it would delay the page by N*INTERVAL and prove nothing new
    about the run. What #2785 added is hysteresis in a different unit — SPEEDTEST_FLOOR_CONSECUTIVE
    counts RESULTS, read from the API's own history in one fetch, so each one is a fresh
    measurement. It applies to the floor arm alone; the status and age arms still judge the
    newest row on sight.

    The FETCH failure does ride a cycle streak, because the app restarting under a deploy is a
    genuine transient — the same split check_ha_heartbeat draws, for the same reason. `speedtest`
    is also in STARTUP_GRACE, which covers the post-reboot cycle where the app has not finished
    booting.
    """
    if not cfg.SPEEDTEST_URL or not cfg.SPEEDTEST_TOKEN:
        return True, "speedtest monitoring disabled (no URL/token)"
    # DECIDED: the verdict reads the REST API, not Prometheus, although Prometheus scrapes the
    # same app (#3105 asked for the switch and this is the recorded reason). Three facts decide
    # it. (1) The scrape carries NO timestamp: `count by (__name__) ({job="speedtest"})` returned
    # 28 names on 2026-10-01 and not one of them is a created-at, age or timestamp series, so the
    # staleness arm — the one SPEEDTEST_MAX_AGE_H exists for, and the only arm whose failure mode
    # (the scheduler dying while the pod stays Ready) nothing else sees — could only be inferred
    # from when `speedtest_tracker_result_id` last changed over an 8h window. (2)
    # SPEEDTEST_FLOOR_CONSECUTIVE counts RESULTS, which the REST history hands back in one fetch;
    # Prometheus samples a 6-hourly result every 5 min, so the same arm would mean grouping
    # samples by result id before it could count anything. (3) The scrape itself depends on a
    # manual UI toggle that roles/k8s/speedtest/CLAUDE.md records as impossible to set from
    # config at the pinned build, on a longhorn-nobackup PVC — a human step this alert path should not
    # acquire. The scrape stays what it was added for in #996: history in Grafana, which a Kuma
    # tile cannot keep.
    try:
        # sort=-created_at, because the default order is ASCENDING and would hand back the
        # OLDEST row in the 30-day window — a stale-forever reading that looks like a verdict.
        # The page holds SPEEDTEST_FLOOR_CONSECUTIVE rows, not one: the floor arm's hysteresis
        # is a run of RESULTS and this fetch is where that history comes from. rows[0] is still
        # the newest, so every other arm is unaffected.
        payload = src.get_json(
            cfg.SPEEDTEST_URL
            + "/api/v1/results?sort=-created_at&page%5Bsize%5D="
            + str(max(1, cfg.SPEEDTEST_FLOOR_CONSECUTIVE)),
            headers={
                "Authorization": "Bearer " + cfg.SPEEDTEST_TOKEN,
                "Accept": "application/json",
            },
        )
    except Exception as e:
        src.state.down_streaks["speedtest"], ok, msg = bridge.streaks.down_streak(
            src.state.down_streaks.get("speedtest", 0),
            cfg.SPEEDTEST_CONSECUTIVE,
            "speedtest API unreachable: %s" % e,
            "deploy/restart grace",
        )
        return ok, msg
    src.state.down_streaks["speedtest"] = 0
    # Narrowed after the streak reset, outside the `try`: a body of the wrong shape is a bug in
    # the reply, not an unreachable API, and must page now rather than ride the deploy grace.
    rows = as_object_list(
        as_object(payload, "speedtest results").get("data") or [],
        "speedtest results data",
    )
    # DECIDED: the check cannot tell a failing Ookla run from a stopped scheduler, and this
    # repo accepts that (#1603). speedtest-tracker writes NO row for a failed run — that is the
    # LinuxServer image's own behaviour, decided between the Ookla CLI exiting non-zero and the
    # results table, and the role has no hook in between. Measured 2026-09-10 against the live
    # app: 25 rows, ids 825-849, every one `status: completed`, so speedtest_verdict's
    # `status != "completed"` branch is reachable by test and has never been taken by data.
    # The two rejected alternatives are a LogQL check for the per-tick log line (Loki does not
    # retain the pod's logs across the restart that is exactly when it is wanted) and replacing
    # the internal scheduler with a k8s CronJob (rejected at the `# DECIDED:` on the
    # SPEEDTEST_SCHEDULE line — it moves the transcript, not the reliability). What mitigates it
    # is the staleness message naming both hypotheses and asserting neither, in
    # verdicts/host.py's speedtest_verdict.
    return speedtest_verdict(
        rows[0] if rows else None,
        cfg.SPEEDTEST_DOWNLOAD_MIN_MBPS,
        cfg.SPEEDTEST_MAX_AGE_H,
        prev_rows=rows[1:],
        floor_consecutive=cfg.SPEEDTEST_FLOOR_CONSECUTIVE,
    )
