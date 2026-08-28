"""Host board/CPU temperature: the two limit arms, and the coverage that must stay exhaustive.

The check landed 2026-08-28. Its failure mode is not a wrong threshold — it is silence. A
sensor that ends up with no limit reads green through a fire, and nothing in production
distinguishes that from a cool machine. So every rule below is a pair (one input it must
accept, one it must reject), and `test_host_temp_covers_every_sensor...` is the one that
matters most: it is the only test that can fail when a future edit narrows coverage.
"""

from pathlib import Path

import check


def _temp(instance, chip, sensor, value):
    """One prom_vector element, in node-exporter's hwmon label shape."""
    return ({"instance": instance, "chip": chip, "sensor": sensor}, value)


# ratio, fallback_c, min_plausible, max_plausible, exclude_chip — the deployed values.
HWMON_ARGS = (0.90, 85.0, 20.0, 150.0, "nvme_")


def _stub_prom(monkeypatch, temps, maxes=()):
    def fake(query, *args, **kwargs):
        if query == "node_hwmon_temp_celsius":
            return list(temps)
        if query == "node_hwmon_temp_max_celsius":
            return list(maxes)
        return []

    monkeypatch.setattr(check, "prom_vector", fake)


def test_declared_max_is_clean_below_the_ratio():
    limits = check.hwmon_temp_limits(
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 63.0)],
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 100.0)],
        *HWMON_ARGS,
    )
    assert limits == [
        ("daniel-server/platform_coretemp_0/temp1", 63.0, 90.0, "declared")
    ]
    ok, msg = check.hwmon_temp_verdict(limits)
    assert ok, msg
    assert "1 by declared max" in msg


def test_declared_max_is_flagged_above_the_ratio():
    limits = check.hwmon_temp_limits(
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 91.0)],
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 100.0)],
        *HWMON_ARGS,
    )
    ok, msg = check.hwmon_temp_verdict(limits)
    assert not ok
    assert "91.0C (limit 90.0C)" in msg


def test_fallback_is_clean_below_the_ceiling():
    limits = check.hwmon_temp_limits(
        [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 61.2)], [], *HWMON_ARGS
    )
    assert limits[0][2] == 85.0
    assert limits[0][3] == "fallback"
    ok, _msg = check.hwmon_temp_verdict(limits)
    assert ok


def test_fallback_is_flagged_above_the_ceiling():
    limits = check.hwmon_temp_limits(
        [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 86.0)], [], *HWMON_ARGS
    )
    ok, msg = check.hwmon_temp_verdict(limits)
    assert not ok
    assert "daniel-pi/thermal_thermal_zone0/temp0" in msg


def test_the_sentinel_max_is_treated_as_undeclared():
    """The regression this check was designed around, measured live on 2026-08-28.

    Three NVMe sensors declare 65261.85 for "no max declared". Trusting it yields a limit of
    58735C, which no temperature reaches — the sensor is then covered on paper and unwatched in
    fact. It must fall to the fallback arm, and that arm must still be able to fire.
    """
    hot = [_temp("daniel-box", "platform_coretemp_0", "temp1", 90.0)]
    sentinel = [_temp("daniel-box", "platform_coretemp_0", "temp1", 65261.85)]
    limits = check.hwmon_temp_limits(hot, sentinel, *HWMON_ARGS)
    assert limits[0][3] == "fallback", (
        "a sentinel max must not be read as a declared limit"
    )
    assert limits[0][2] == 85.0
    ok, _msg = check.hwmon_temp_verdict(limits)
    assert not ok, "the sentinel arm must still be able to go RED"


def test_an_implausibly_low_max_is_also_rejected():
    """Bounded at both ends: a max at or below the floor would page on an idle machine."""
    limits = check.hwmon_temp_limits(
        [_temp("daniel-box", "thermal_thermal_zone0", "temp0", 20.0)],
        [_temp("daniel-box", "thermal_thermal_zone0", "temp0", 5.0)],
        *HWMON_ARGS,
    )
    assert limits[0][3] == "fallback"
    ok, _msg = check.hwmon_temp_verdict(limits)
    assert ok, "an idle sensor must not page because its declared max was nonsense"


def test_drive_chips_are_left_to_scrutiny():
    limits = check.hwmon_temp_limits(
        [
            _temp("daniel-box", "nvme_nvme0", "temp1", 99.0),
            _temp("daniel-box", "platform_coretemp_0", "temp1", 40.0),
        ],
        [],
        *HWMON_ARGS,
    )
    labels = [la for la, _t, _li, _b in limits]
    assert labels == ["daniel-box/platform_coretemp_0/temp1"], (
        "drive temperature belongs to check_scrutiny, whose device_status folds the SMART "
        "temperature attribute; reading it here double-pages one condition"
    )


def test_covers_every_sensor_it_does_not_deliberately_exclude():
    """The anti-silence guard: no scraped sensor may end up without a limit.

    Built from the live 2026-08-28 label shapes — declared, sentinel and absent maxes mixed
    across all three hosts. A future edit that narrows the join drops this count and fails
    here, instead of going quiet in production.
    """
    temps = [
        _temp("daniel-server", "platform_coretemp_0", "temp1", 63.0),
        _temp("daniel-server", "thermal_thermal_zone0", "temp0", 54.0),
        _temp("daniel-server", "nvme_nvme0", "temp2", 44.85),
        _temp("daniel-box", "ieee80211_phy0", "temp1", 46.0),
        _temp("daniel-box", "thermal_thermal_zone0", "temp0", 20.0),
        _temp("daniel-pi", "thermal_thermal_zone0", "temp0", 61.2),
    ]
    maxes = [
        _temp("daniel-server", "platform_coretemp_0", "temp1", 100.0),
        _temp("daniel-server", "nvme_nvme0", "temp2", 65261.85),
    ]
    limits = check.hwmon_temp_limits(temps, maxes, *HWMON_ARGS)
    excluded = [t for t in temps if "nvme_" in t[0]["chip"]]
    assert len(limits) == len(temps) - len(excluded), (
        "every non-excluded sensor must carry a limit — an uncovered sensor reads green forever"
    )
    assert all(limit > 0 for _la, _t, limit, _b in limits)
    assert {basis for _la, _t, _li, basis in limits} == {"declared", "fallback"}, (
        "both arms must be live in a realistic estate; one arm going empty means the other "
        "silently became the whole check"
    )


def test_no_sensors_scraped_pages_rather_than_passing():
    ok, msg = check.hwmon_temp_verdict([])
    assert not ok
    assert "collector blind" in msg


def test_a_single_spike_is_held_and_sustained_heat_pages(monkeypatch):
    """Hysteresis, both halves: one hot cycle must not page, the Nth must."""
    check._down_streaks.pop("host_temp", None)
    _stub_prom(
        monkeypatch, [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 99.0)]
    )
    results = [check.check_host_temp() for _ in range(check.HWMON_TEMP_CONSECUTIVE)]
    assert all(ok for ok, _msg in results[:-1]), (
        "a one-cycle thermal spike must not page"
    )
    assert not results[-1][0], "sustained heat must page on the Nth consecutive cycle"


def test_a_clean_cycle_clears_the_streak(monkeypatch):
    check._down_streaks["host_temp"] = 2
    _stub_prom(
        monkeypatch, [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 40.0)]
    )
    ok, _msg = check.check_host_temp()
    assert ok
    assert check._down_streaks["host_temp"] == 0, (
        "a clean cycle must reset the hysteresis"
    )


def test_registered_tokened_and_prom_suppressed():
    """Registration, token and suppression are one unit — any one alone is a broken monitor."""
    names = {name for name, _token, _fn in check.CHECKS}
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    registered = "host_temp" in names
    assert registered, "an unregistered check never runs; it would be dead code"
    assert registered == ("KUMA_PUSH_HOST_TEMP" in env_secret), (
        "the CHECKS entry and the KUMA_PUSH_HOST_TEMP env-secret key move together — one "
        "without the other is either a check that cannot page or a token nothing reads"
    )
    assert "host_temp" in check.PROM_DEPENDENT, (
        "it reads Prometheus and pages on an empty vector, so a Prometheus outage must "
        "suppress it — otherwise one outage pages here and on the Prometheus monitor both"
    )


def test_the_kuma_tile_exists_for_the_token():
    """The tile deploys from uptime-kuma and the pusher from monitor-bridge — the pair that drifts."""
    tile = (
        Path(check.__file__).resolve().parents[2]
        / "uptime-kuma"
        / "templates"
        / "static-monitors.yaml.j2"
    ).read_text()
    assert "monitor_bridge_host_temp_push_token" in tile, (
        "a token with no Kuma tile pushes into the void"
    )
