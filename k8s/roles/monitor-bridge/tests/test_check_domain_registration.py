"""The domain registration row: the RDAP read, its day-long cache, and the days-left gauge.

`domain_registration` reads through a `FakeSources`, and the cache is a fresh dict per test, so
nothing here patches the module.
"""

import os
import re
from dataclasses import replace

import pytest

import checks.domain_registration as dr
import cli
import deploy_pipeline
import verdict_export
from _bridge_env import bridge_env
from _fake_sources import FakeSources
from bridge.streaks import RegistrationCache
from bridge.types import Check, JsonValue

EXPIRY = "2027-01-20T19:24:49Z"
EXPIRY_TS = 1800473089.0
RECORD: JsonValue = {
    "events": [
        {"eventAction": "registration", "eventDate": "2022-01-20T19:24:49Z"},
        {"eventAction": "expiration", "eventDate": EXPIRY},
    ]
}
GAUGE = re.compile(r"^homelab_domain_registration_expiry_days (\S+)$", re.M)


def _days(export) -> float | None:
    """The days-left gauge in the exporter's served body, or None when it serves none."""
    found = GAUGE.search(export.body().decode())
    return None if found is None else float(found.group(1))


def _fresh() -> RegistrationCache:
    return {"ts": None, "ok": False, "expiry": None}


def _answering(*answers):
    """A FakeSources whose successive `get_json` calls return, or raise, each answer."""
    it = iter(answers)

    def get_json(url, headers=None):
        answer = next(it)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return FakeSources(get_json=get_json)


def test_the_expiration_event_dates_the_registration():
    assert dr.expiry_from_rdap(RECORD) == EXPIRY_TS


def test_a_record_without_an_expiration_event_is_malformed():
    with pytest.raises(ValueError, match="no expiration event"):
        dr.expiry_from_rdap({"events": [{"eventAction": "registration"}]})


def test_a_read_reports_the_days_left_and_caches_the_expiry(cfg):
    probe = _fresh()
    now = EXPIRY_TS - 30 * dr.DAY_S
    ok, msg = dr.domain_registration(cfg, _answering(RECORD), now=now, probe=probe)
    assert ok
    assert "expires in 30 days" in msg
    assert dr.days_remaining(probe, now) == 30.0


def test_a_success_is_not_read_again_within_the_interval(cfg):
    probe = _fresh()
    src = _answering(RECORD)
    dr.domain_registration(cfg, src, now=1000.0, probe=probe)
    later = 1000.0 + cfg.DOMAIN_REGISTRATION_PROBE_INTERVAL_S - 1
    ok, msg = dr.domain_registration(cfg, src, now=later, probe=probe)
    assert ok
    assert "read 24h ago" in msg
    assert len(src.calls) == 1


def test_a_failed_read_is_down_retried_and_keeps_the_last_expiry(cfg):
    probe = _fresh()
    dr.domain_registration(cfg, _answering(RECORD), now=1000.0, probe=probe)
    stale = 1000.0 + cfg.DOMAIN_REGISTRATION_PROBE_INTERVAL_S
    src = _answering(RuntimeError("HTTP 503"), RECORD)
    ok, msg = dr.domain_registration(cfg, src, now=stale, probe=probe)
    assert not ok
    assert "HTTP 503" in msg
    assert probe["expiry"] == EXPIRY_TS
    assert dr.domain_registration(cfg, src, now=stale + 300, probe=probe)[0]
    assert len(src.calls) == 2


def test_an_empty_url_disables_the_check_without_a_read(cfg):
    ok, msg = dr.domain_registration(
        replace(cfg, DOMAIN_RDAP_URL=""), FakeSources(), now=1000.0, probe=_fresh()
    )
    assert ok
    assert "disabled" in msg


def test_the_exporter_serves_the_days_left_only_once_set():
    export = verdict_export.CheckExposition(300)
    assert _days(export) is None
    export.set_registration_days(30.5)
    assert _days(export) == 30.5


def test_the_loop_exports_the_days_left_from_the_cached_expiry():
    class Stop(Exception):
        pass

    def stop(_seconds):
        raise Stop

    sources = FakeSources(prom_vector=lambda q: [])
    sources.state.domain_registration_probe["expiry"] = 4102444800.0
    export = verdict_export.CheckExposition(300)
    with pytest.raises(Stop):
        cli.main(
            argv=[],
            env=bridge_env(HEARTBEAT_FILE=os.devnull),
            checks=[Check("noop", lambda _cfg, _src: (True, "ok"))],
            sources=sources,
            sleep=stop,
            exposition=deploy_pipeline.Exposition(),
            check_exposition=export,
        )
    days = _days(export)
    assert days is not None and 0 < days < (4102444800.0 / dr.DAY_S)
