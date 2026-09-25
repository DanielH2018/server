"""The Healthchecks.io console against the deadman doc's schedules and graces (#2566).

`healthchecks_verdict` is pure. The flagged cases are the two real drifts #2563 found:
`pi-peer-backup` carrying `30 23 * * *` after its CronJob moved to `0 23`, and
`longhorn-backup-health` set up as a daily Cron check instead of Simple 10m/20m. The fetch and
the cache are parameters of `healthchecks_drift`, so nothing here patches the module.
"""

from dataclasses import replace
import json

from bridge.config import load_config
import checks.healthchecks as hc

EXPECTED = (
    {"slug": "longhorn-backup-health", "kind": "simple", "timeout": 600, "grace": 1200},
    {"slug": "daniel-box-disk-health", "kind": "simple", "timeout": 600, "grace": 1500},
    {
        "slug": "pi-peer-backup",
        "kind": "cron",
        "schedule": "0 23 * * *",
        "tz": "America/Chicago",
        "grace": 7200,
    },
)

# The reconciled console, shaped as the v3 API answers a read-only key.
CONSOLE = [
    {
        "name": "Longhorn backup health",
        "slug": "longhorn-backup-health",
        "timeout": 600,
        "grace": 1200,
    },
    {"name": "Disk", "slug": "daniel-box-disk-health", "timeout": 600, "grace": 1500},
    {
        "name": "Pi peer backup",
        "slug": "pi-peer-backup",
        "schedule": "0 23 * * *",
        "tz": "America/Chicago",
        "grace": 7200,
    },
]


def _with(slug, **fields):
    """CONSOLE with one check's fields replaced (a key set to None is dropped)."""
    out = []
    for c in CONSOLE:
        if c["slug"] == slug:
            c = {k: v for k, v in {**c, **fields}.items() if v is not None}
        out.append(c)
    return out


def _armed(cfg):
    return replace(cfg, HEALTHCHECKS_API_KEY="k", HEALTHCHECKS_EXPECTED=EXPECTED)


def _fresh_probe() -> dict:
    return {"ts": None, "ok": True, "msg": ""}


def test_the_reconciled_console_is_clean():
    ok, msg = hc.healthchecks_verdict(CONSOLE, list(EXPECTED))
    assert ok
    assert "matches the doc (3 checks)" in msg


def test_pi_peer_backup_on_the_old_cron_is_flagged():
    ok, msg = hc.healthchecks_verdict(
        _with("pi-peer-backup", schedule="30 23 * * *"), list(EXPECTED)
    )
    assert not ok
    assert "pi-peer-backup: schedule `30 23 * * *`, expected `0 23 * * *`" in msg


def test_a_changed_grace_is_flagged_naming_the_slug():
    # The issue's Verify-by: change a console grace, and the check pages naming that slug.
    ok, msg = hc.healthchecks_verdict(
        _with("daniel-box-disk-health", grace=1800), list(EXPECTED)
    )
    assert not ok
    assert "daniel-box-disk-health: grace 1800s, expected 1500s" in msg


def test_a_simple_check_turned_cron_is_flagged():
    ok, msg = hc.healthchecks_verdict(
        _with(
            "longhorn-backup-health",
            timeout=None,
            schedule="0 23 * * *",
            tz="America/Chicago",
        ),
        list(EXPECTED),
    )
    assert not ok
    assert (
        "longhorn-backup-health: Cron `0 23 * * *` America/Chicago, expected Simple 600s"
        in msg
    )


def test_a_cron_check_in_the_wrong_timezone_is_flagged():
    ok, msg = hc.healthchecks_verdict(_with("pi-peer-backup", tz="UTC"), list(EXPECTED))
    assert not ok
    assert "pi-peer-backup: tz UTC, expected America/Chicago" in msg


def test_a_documented_check_missing_from_the_console_is_flagged():
    ok, msg = hc.healthchecks_verdict(CONSOLE[1:], list(EXPECTED))
    assert not ok
    assert "longhorn-backup-health: missing from the console" in msg


def test_an_undocumented_console_check_is_named_but_clean():
    extra = CONSOLE + [{"slug": "scratch", "timeout": 60, "grace": 60}]
    ok, msg = hc.healthchecks_verdict(extra, list(EXPECTED))
    assert ok
    assert "undocumented: scratch" in msg


def test_disabled_without_a_key(cfg):
    ok, msg = hc.healthchecks_drift(
        replace(cfg, HEALTHCHECKS_EXPECTED=EXPECTED), probe=_fresh_probe()
    )
    assert ok and "disabled" in msg


def test_a_success_is_cached_for_the_interval(cfg):
    cfg = _armed(cfg)
    calls: list[int] = []

    def fetch(_cfg):
        calls.append(1)
        return CONSOLE

    probe = _fresh_probe()
    assert hc.healthchecks_drift(cfg, now=1000.0, fetch=fetch, probe=probe)[0]
    ok, msg = hc.healthchecks_drift(
        cfg,
        now=1000.0 + cfg.HEALTHCHECKS_PROBE_INTERVAL_S - 1,
        fetch=fetch,
        probe=probe,
    )
    assert ok and "checked" in msg
    assert len(calls) == 1


def test_a_failed_read_is_down_and_reprobed_next_cycle(cfg):
    cfg = _armed(cfg)
    calls: list[int] = []

    def boom(_cfg):
        calls.append(1)
        raise RuntimeError("healthchecks.io: HTTP Error 401")

    probe = _fresh_probe()
    ok, msg = hc.healthchecks_drift(cfg, now=1000.0, fetch=boom, probe=probe)
    assert not ok and "unverified" in msg and "401" in msg
    assert not hc.healthchecks_drift(cfg, now=1300.0, fetch=boom, probe=probe)[0]
    assert len(calls) == 2


def test_the_expected_list_parses_from_the_env():
    cfg = load_config({"HEALTHCHECKS_EXPECTED": json.dumps(list(EXPECTED))})
    assert cfg.HEALTHCHECKS_EXPECTED == EXPECTED
    assert cfg.CONFIG_PROBLEMS == ()


def test_a_malformed_expected_list_is_a_config_problem():
    cfg = load_config({"HEALTHCHECKS_EXPECTED": '[{"slug": "x"}]'})
    assert cfg.HEALTHCHECKS_EXPECTED == ()
    assert any("HEALTHCHECKS_EXPECTED is malformed" in p for p in cfg.CONFIG_PROBLEMS)
