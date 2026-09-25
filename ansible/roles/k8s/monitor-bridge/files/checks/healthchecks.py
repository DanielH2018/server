"""The Healthchecks.io console against the schedules and graces the deadman doc declares.

docs/healthchecks-io-deadman.md is the record of what each off-site dead-man check should carry,
but the values themselves live only in the Healthchecks.io console, which nothing in the repo can
set. The console drifted twice before anything noticed (#2563): `pi-peer-backup` kept `30 23`
after its CronJob moved to `0 23` and went DOWN every morning, and `longhorn-backup-health` sat as
a daily Cron check, so its dead-man arm needed a day to notice silence instead of 30 minutes.

This reads `GET /api/v3/checks/` with the project's READ-ONLY key and compares every documented
slug's schedule type, period or cron expression, timezone and grace against
`HEALTHCHECKS_EXPECTED`, which the env Secret renders from monitor-bridge's
`monitor_bridge_healthchecks_expected`. Tests tie that list to the cron variables and to the
doc's table (ansible/tests/services/test_monitor_bridge_healthchecks_expected.py).

Same cache idiom as `checks/cloudflare_ips.py`: a success is cached for
HEALTHCHECKS_PROBE_INTERVAL_S (a day), a failure is never cached, so a red tile re-probes every
cycle and clears one cycle after the console is fixed.
"""

from collections.abc import Callable, Sequence
import time

import bridge.net
from bridge.config import Config


def fetch_checks(cfg: Config) -> list[dict]:
    """The project's checks as the v3 API returns them. Raises on any transport or HTTP error.

    A read-only key gets the same fields a read-write key does minus the ping and management
    URLs: `slug`, `grace`, and `timeout` on a Simple check or `schedule` + `tz` on a Cron one.
    """
    body = bridge.net._get_json(
        cfg.HEALTHCHECKS_API_URL, {"X-Api-Key": cfg.HEALTHCHECKS_API_KEY}
    )
    checks = body.get("checks") if isinstance(body, dict) else None
    if not isinstance(checks, list):
        raise RuntimeError("response carries no `checks` list")
    return checks


def _describe(check: dict) -> str:
    if "timeout" in check:
        return "Simple %ss" % check["timeout"]
    return "Cron `%s` %s" % (check.get("schedule"), check.get("tz"))


def _slug_problems(want: dict, got: dict) -> list[str]:
    """Every field of one console check that differs from its expectation, as `field got≠want`."""
    slug = want["slug"]
    if want["kind"] == "simple":
        if "timeout" not in got:
            return [
                "%s: %s, expected Simple %ss" % (slug, _describe(got), want["timeout"])
            ]
        problems = []
        if int(got["timeout"]) != int(want["timeout"]):
            problems.append(
                "%s: period %ss, expected %ss" % (slug, got["timeout"], want["timeout"])
            )
    else:
        if "schedule" not in got:
            return [
                "%s: %s, expected Cron `%s` %s"
                % (slug, _describe(got), want["schedule"], want["tz"])
            ]
        problems = []
        if " ".join(str(got["schedule"]).split()) != " ".join(want["schedule"].split()):
            problems.append(
                "%s: schedule `%s`, expected `%s`"
                % (slug, got["schedule"], want["schedule"])
            )
        if got.get("tz") != want["tz"]:
            problems.append(
                "%s: tz %s, expected %s" % (slug, got.get("tz"), want["tz"])
            )
    if int(got.get("grace", -1)) != int(want["grace"]):
        problems.append(
            "%s: grace %ss, expected %ss" % (slug, got.get("grace"), want["grace"])
        )
    return problems


def healthchecks_verdict(
    live: list[dict], expected: Sequence[dict]
) -> tuple[bool, str]:
    """(ok, msg) for the console's checks against the documented expectations. Pure."""
    by_slug = {c.get("slug"): c for c in live}
    problems: list[str] = []
    for want in expected:
        got = by_slug.get(want["slug"])
        if got is None:
            problems.append("%s: missing from the console" % want["slug"])
        else:
            problems.extend(_slug_problems(want, got))
    # DECIDED: an undocumented console check is named but does not page. Adding a check cannot
    # silence a documented dead-man, and this tile answers whether those are configured as the
    # doc says; a console experiment would otherwise page until the doc caught up.
    known = {w["slug"] for w in expected}
    extra = sorted(str(s) for s in by_slug if s not in known)
    extra_note = " (undocumented: %s)" % ", ".join(extra) if extra else ""
    if problems:
        return False, (
            "Healthchecks.io console DRIFTED from docs/healthchecks-io-deadman.md — %s — fix "
            "the console, or the doc and monitor_bridge_healthchecks_expected together%s"
            % ("; ".join(problems), extra_note)
        )
    return True, "Healthchecks.io console matches the doc (%d checks)%s" % (
        len(expected),
        extra_note,
    )


# ts=None means never probed; see checks/r2.py for why not 0.0.
_probe = {"ts": None, "ok": True, "msg": ""}


def healthchecks_drift(
    cfg: Config,
    now: float | None = None,
    fetch: Callable[[Config], list[dict]] = fetch_checks,
    probe: dict | None = None,
) -> tuple[bool, str]:
    """Throttled console drift check. (ok, msg).

    `fetch` and `probe` are the seams, as in `checks/cloudflare_ips.py`: a test passes a canned
    fetch and a fresh cache dict rather than patching this module.
    """
    probe = _probe if probe is None else probe
    if not cfg.HEALTHCHECKS_API_KEY or not cfg.HEALTHCHECKS_EXPECTED:
        return (
            True,
            "Healthchecks.io drift check disabled (no API key or expected list)",
        )
    now = now if now is not None else time.time()
    if (
        probe["ts"] is not None
        and probe["ok"]
        and now - probe["ts"] < cfg.HEALTHCHECKS_PROBE_INTERVAL_S
    ):
        return True, "%s (checked %.0fh ago)" % (
            probe["msg"],
            (now - probe["ts"]) / 3600,
        )
    try:
        live = fetch(cfg)
    except Exception as e:
        ok, msg = (
            False,
            "failed to read the Healthchecks.io console — unverified: %s" % e,
        )
    else:
        ok, msg = healthchecks_verdict(live, cfg.HEALTHCHECKS_EXPECTED)
    probe["ts"] = now
    probe["ok"] = ok
    probe["msg"] = msg
    return ok, msg


def check_healthchecks_drift(cfg: Config) -> tuple[bool, str]:
    return healthchecks_drift(cfg)
