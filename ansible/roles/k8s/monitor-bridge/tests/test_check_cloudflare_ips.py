"""The cloudflare_ips allowlist against Cloudflare's published ranges.

`cloudflare_ips_verdict` is pure and decides drift, a match, or an implausibly short page.
`cloudflare_ips_drift` caches a success for a day and re-probes after any failure, which is
the whole reason the check moved here from a daily host cron: a red tile clears one cycle
after the list is fixed instead of at the next day's slot. The fetch and the cache are
parameters of the check, so nothing here patches the module.
"""

from dataclasses import replace

import checks.cloudflare_ips as cf

V4 = [f"198.51.{i}.0/24" for i in range(8)]
V6 = [f"2001:db8:{i:x}::/48" for i in range(4)]
PUBLISHED = V4 + V6


def _armed(cfg, expected=None):
    return replace(cfg, CLOUDFLARE_IPS_EXPECTED=frozenset(expected or PUBLISHED))


def _fresh_probe() -> dict:
    return {"ts": None, "ok": True, "msg": ""}


def _canned(ranges, calls):
    def fetch():
        calls.append(1)
        return list(ranges)

    return fetch


def test_a_matching_list_is_up():
    ok, msg = cf.cloudflare_ips_verdict(PUBLISHED, frozenset(PUBLISHED))
    assert ok
    assert "matches upstream (12 CIDRs)" in msg


def test_a_range_cloudflare_added_is_drift_naming_it():
    ok, msg = cf.cloudflare_ips_verdict(
        PUBLISHED + ["203.0.113.0/24"], frozenset(PUBLISHED)
    )
    assert not ok
    assert "added:[203.0.113.0/24]" in msg
    assert "stale:[]" in msg


def test_a_range_cloudflare_dropped_is_drift_naming_it():
    ok, msg = cf.cloudflare_ips_verdict(PUBLISHED[1:], frozenset(PUBLISHED))
    assert not ok
    assert f"stale:[{PUBLISHED[0]}]" in msg


def test_an_implausibly_short_page_is_a_bad_fetch_not_a_shrunken_list():
    ok, msg = cf.cloudflare_ips_verdict(PUBLISHED[:3], frozenset(PUBLISHED))
    assert not ok
    assert "implausible" in msg


def test_disabled_without_an_expected_list(cfg):
    ok, msg = cf.cloudflare_ips_drift(
        replace(cfg, CLOUDFLARE_IPS_EXPECTED=frozenset()), probe=_fresh_probe()
    )
    assert ok and "disabled" in msg


def test_a_success_is_cached_for_the_interval(cfg):
    cfg = _armed(cfg)
    calls: list[int] = []
    probe = _fresh_probe()
    fetch = _canned(PUBLISHED, calls)
    assert cf.cloudflare_ips_drift(cfg, now=1000.0, fetch=fetch, probe=probe)[0]
    ok, msg = cf.cloudflare_ips_drift(
        cfg,
        now=1000.0 + cfg.CLOUDFLARE_IPS_PROBE_INTERVAL_S - 1,
        fetch=fetch,
        probe=probe,
    )
    assert ok
    assert len(calls) == 1
    assert "checked" in msg


def test_a_failure_reprobes_next_cycle_and_clears_on_the_fix(cfg):
    # The sticky-DOWN case this check exists to shorten: drift pushes down, and the next cycle
    # after the allowlist is fixed pushes up, with no day-long wait in between.
    cfg = _armed(cfg)
    calls: list[int] = []
    probe = _fresh_probe()
    fetch = _canned(PUBLISHED + ["203.0.113.0/24"], calls)
    assert not cf.cloudflare_ips_drift(cfg, now=1000.0, fetch=fetch, probe=probe)[0]
    assert not cf.cloudflare_ips_drift(cfg, now=1300.0, fetch=fetch, probe=probe)[0]
    assert len(calls) == 2
    fixed = _armed(cfg, PUBLISHED + ["203.0.113.0/24"])
    assert cf.cloudflare_ips_drift(fixed, now=1600.0, fetch=fetch, probe=probe)[0]


def test_a_fetch_that_does_not_answer_is_down_and_not_cached(cfg):
    cfg = _armed(cfg)
    calls: list[int] = []
    probe = _fresh_probe()

    def boom():
        calls.append(1)
        raise RuntimeError("ips-v6: timed out")

    ok, msg = cf.cloudflare_ips_drift(cfg, now=1000.0, fetch=boom, probe=probe)
    assert not ok and "unverified" in msg and "ips-v6" in msg
    assert not cf.cloudflare_ips_drift(cfg, now=1300.0, fetch=boom, probe=probe)[0]
    assert len(calls) == 2
