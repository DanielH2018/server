"""monitor-bridge's Healthchecks.io expectations agree with the crons that ping each slug.

`monitor_bridge_healthchecks_expected` is what checks/healthchecks.py holds the live console to.
It is a literal because the cron variables live in three other roles' defaults, which
the deploy play cannot read from monitor-bridge's templates. This guard is what stops the
literal drifting from its sources: each slug's period or cron expression must equal what the
deadman-cadences fragment assembles from the cron variables. The deadman-graces fragment of
docs/healthchecks-io-deadman.md renders each slug's schedule type and grace from the same
literal, so the doc cannot differ from it.

Run: uv run pytest ansible/tests/services/test_monitor_bridge_healthchecks_expected.py
"""

import json
import re

from _helpers import REPO
from _k8s_render import rendered_docs
from fragment_renderers import deadman_crons
from gen_doc_fragments import deadman_inputs
from lib.estate import role_defaults

BRIDGE_ROLE = REPO / "ansible/roles/k8s/monitor-bridge"

# The census the comparisons below must cover, so an empty list cannot pass them vacuously.
WIRED_SLUGS = frozenset(
    {
        "longhorn-backup-health",
        "daniel-box-disk-health",
        "uptime-kuma-alive",
        "alertmanager-watchdog",
        "manifest-prune-check",
        "etcd-snapshot-offbox",
        "pi-peer-backup",
        "registry-gc",
        "weekly-reboot-daniel-box",
        "weekly-reboot-daniel-server",
        "weekly-reboot-daniel-pi",
        "daniel-pi-docker-prune",
        "daniel-box-worktree-sweep",
    }
)

_EVERY_N_MINUTES = re.compile(r"^\*/(\d+) \* \* \* \*$")
# A sender that is not a cron, such as Alertmanager's Watchdog repeat, pings every N seconds.
_EVERY_N_SECONDS = re.compile(r"^every (\d+)s$")


def _expected() -> dict[str, dict]:
    rows = role_defaults(BRIDGE_ROLE)["monitor_bridge_healthchecks_expected"]
    return {r["slug"]: r for r in rows}


def _crons() -> dict[str, str]:
    rows = deadman_crons(*deadman_inputs())
    return {slug: cron for slug, cron, _ in rows}


def cron_mismatches(expected: dict[str, dict], crons: dict[str, str]) -> list[str]:
    """Every slug whose expected period or cron expression differs from its cron variable.

    A Simple check fed by a `*/N` minute cron must have a period of exactly N minutes. One fed
    `every <N>s` must have a period above N, so a single late send cannot page; its period is
    the slug's `timeout`, not N.
    """
    out = []
    for slug, want in expected.items():
        cron = crons.get(slug)
        if want["kind"] == "simple":
            if m := _EVERY_N_SECONDS.match(cron or ""):
                if int(m.group(1)) >= want["timeout"]:
                    out.append(
                        "%s: Simple %ss against a send every %ss"
                        % (slug, want["timeout"], m.group(1))
                    )
                continue
            m = _EVERY_N_MINUTES.match(cron or "")
            if not m or int(m.group(1)) * 60 != want["timeout"]:
                out.append(
                    "%s: Simple %ss against cron %r" % (slug, want["timeout"], cron)
                )
        elif want["schedule"] != cron:
            out.append("%s: Cron %r against cron %r" % (slug, want["schedule"], cron))
    return out


def test_the_expectations_cover_exactly_the_wired_slugs():
    assert set(_expected()) == WIRED_SLUGS
    assert set(_crons()) == WIRED_SLUGS


def test_every_expectation_matches_the_cron_that_pings_it_is_clean():
    assert cron_mismatches(_expected(), _crons()) == []


def test_a_send_every_n_seconds_under_the_period_is_clean():
    # The live row: the Watchdog repeats every 60s against the check's 600s period.
    assert _crons()["alertmanager-watchdog"] == "every 60s"
    assert (
        cron_mismatches(
            {"alertmanager-watchdog": _expected()["alertmanager-watchdog"]}, _crons()
        )
        == []
    )


def test_a_send_every_n_seconds_at_the_period_is_flagged():
    expected = {"alertmanager-watchdog": _expected()["alertmanager-watchdog"]}
    assert cron_mismatches(expected, {"alertmanager-watchdog": "every 600s"}) == [
        "alertmanager-watchdog: Simple 600s against a send every 600s"
    ]


def test_a_cron_moved_without_the_expectation_is_flagged():
    # The repo side of a drift: the CronJob moved to `0 23`, the expectation did not.
    expected = _expected()
    expected["pi-peer-backup"] = dict(
        expected["pi-peer-backup"], schedule="30 23 * * *"
    )
    assert cron_mismatches(expected, _crons()) == [
        "pi-peer-backup: Cron '30 23 * * *' against cron '0 23 * * *'"
    ]


def test_only_pi_peer_backup_runs_in_the_containers_timezone():
    # pi-peer-backup is a k8s CronJob scheduled in `tz`; every other slug is a UTC host cron.
    # Both sides are read as rendered: the expectation as the bridge's Deployment env carries it,
    # the zone from the CronJob's spec.
    expected = cron_zone = None
    for role, _tpl, doc in rendered_docs():
        if role == "monitor-bridge" and doc.get("kind") == "Deployment":
            env = doc["spec"]["template"]["spec"]["containers"][0].get("env", [])
            raw = next(
                (e["value"] for e in env if e["name"] == "HEALTHCHECKS_EXPECTED"), None
            )
            expected = json.loads(raw) if raw else expected
        elif role == "pi-peer-backup" and doc.get("kind") == "CronJob":
            cron_zone = doc["spec"]["timeZone"]
    assert expected and cron_zone, (
        "the render carried no HEALTHCHECKS_EXPECTED or CronJob"
    )
    tzs = {r["slug"]: r.get("tz") for r in expected if r["kind"] == "cron"}
    assert tzs.pop("pi-peer-backup") == cron_zone
    assert set(tzs.values()) == {"UTC"}
