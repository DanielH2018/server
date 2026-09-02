"""Host board/CPU temperature: the two limit arms, and the coverage that must stay exhaustive.

The check landed 2026-08-28. Its failure mode is not a wrong threshold — it is silence. A
sensor that ends up with no limit reads green through a fire, and nothing in production
distinguishes that from a cool machine. So every rule below is a pair (one input it must
accept, one it must reject), and `test_host_temp_covers_every_sensor...` is the one that
matters most: it is the only test that can fail when a future edit narrows coverage.
"""

from pathlib import Path

import bridge_config
import bridge_streaks
import bridge_io
import checks_host
import check
import pytest


def _temp(instance, chip, sensor, value):
    """One prom_vector element, in node-exporter's hwmon label shape."""
    return ({"instance": instance, "chip": chip, "sensor": sensor}, value)


# ratio, fallback_c, min_plausible, max_plausible, exclude_chip — the deployed values.
HWMON_ARGS = (0.90, 85.0, 20.0, 150.0, "nvme_")


def _stub_prom(monkeypatch, temps, maxes=(), chip_names=(), sensor_labels=()):
    def fake(query, *args, **kwargs):
        if query == "node_hwmon_temp_celsius":
            return list(temps)
        if query == "node_hwmon_temp_max_celsius":
            return list(maxes)
        if query == "node_hwmon_chip_names":
            return list(chip_names)
        if query == "node_hwmon_sensor_label":
            return list(sensor_labels)
        return []

    monkeypatch.setattr(bridge_io, "prom_vector", fake)


def test_declared_max_is_clean_below_the_ratio():
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 63.0)],
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 100.0)],
        *HWMON_ARGS,
    )
    assert limits == [
        ("daniel-server platform_coretemp_0/temp1", 63.0, 90.0, "declared")
    ]
    ok, msg = checks_host.hwmon_temp_verdict(limits)
    assert ok, msg
    assert "1 by declared max" in msg


def test_declared_max_is_flagged_above_the_ratio():
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 91.0)],
        [_temp("daniel-server", "platform_coretemp_0", "temp1", 100.0)],
        *HWMON_ARGS,
    )
    ok, msg = checks_host.hwmon_temp_verdict(limits)
    assert not ok
    assert "91.0C over its 90.0C declared limit" in msg
    assert msg.startswith("1 of 1 sensors over limit:"), (
        "the breach leads; the coverage tally must not sit inside the sentence naming it"
    )


def test_fallback_is_clean_below_the_ceiling():
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 61.2)], [], *HWMON_ARGS
    )
    assert limits[0][2] == 85.0
    assert limits[0][3] == "fallback"
    ok, _msg = checks_host.hwmon_temp_verdict(limits)
    assert ok


def test_fallback_is_flagged_above_the_ceiling():
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 86.0)], [], *HWMON_ARGS
    )
    ok, msg = checks_host.hwmon_temp_verdict(limits)
    assert not ok
    assert "daniel-pi thermal_thermal_zone0/temp0" in msg
    assert "fallback limit" in msg, (
        "a fallback breach reads differently from a declared one — the flat ceiling may simply "
        "not suit a chip that declares no max, so the message must say which arm set the limit"
    )


def test_the_sentinel_max_is_treated_as_undeclared():
    """The regression this check was designed around, measured live on 2026-08-28.

    Three NVMe sensors declare 65261.85 for "no max declared". Trusting it yields a limit of
    58735C, which no temperature reaches — the sensor is then covered on paper and unwatched in
    fact. It must fall to the fallback arm, and that arm must still be able to fire.
    """
    hot = [_temp("daniel-box", "platform_coretemp_0", "temp1", 90.0)]
    sentinel = [_temp("daniel-box", "platform_coretemp_0", "temp1", 65261.85)]
    limits = checks_host.hwmon_temp_limits(hot, sentinel, *HWMON_ARGS)
    assert limits[0][3] == "fallback", (
        "a sentinel max must not be read as a declared limit"
    )
    assert limits[0][2] == 85.0
    ok, _msg = checks_host.hwmon_temp_verdict(limits)
    assert not ok, "the sentinel arm must still be able to go RED"


def test_an_implausibly_low_max_is_also_rejected():
    """Bounded at both ends: a max at or below the floor would page on an idle machine."""
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-box", "thermal_thermal_zone0", "temp0", 20.0)],
        [_temp("daniel-box", "thermal_thermal_zone0", "temp0", 5.0)],
        *HWMON_ARGS,
    )
    assert limits[0][3] == "fallback"
    ok, _msg = checks_host.hwmon_temp_verdict(limits)
    assert ok, "an idle sensor must not page because its declared max was nonsense"


def test_drive_chips_are_left_to_scrutiny():
    limits = checks_host.hwmon_temp_limits(
        [
            _temp("daniel-box", "nvme_nvme0", "temp1", 99.0),
            _temp("daniel-box", "platform_coretemp_0", "temp1", 40.0),
        ],
        [],
        *HWMON_ARGS,
    )
    labels = [la for la, _t, _li, _b in limits]
    assert labels == ["daniel-box platform_coretemp_0/temp1"], (
        "drive temperature belongs to check_scrutiny, whose device_status folds the SMART "
        "temperature attribute; reading it here double-pages one condition"
    )


def _chip_name(instance, chip, name):
    return ({"instance": instance, "chip": chip, "chip_name": name}, 1.0)


def _sensor_label(instance, chip, sensor, label):
    return (
        {"instance": instance, "chip": chip, "sensor": sensor, "label": label},
        1.0,
    )


def test_a_named_sensor_reads_as_its_hardware_name():
    """`daniel-box/pci0000:00_0000:00:18_3/temp1` is the sysfs path for k10temp's Tctl."""
    names = checks_host.hwmon_name_maps(
        [_chip_name("daniel-box", "pci0000:00_0000:00:18_3", "k10temp")],
        [_sensor_label("daniel-box", "pci0000:00_0000:00:18_3", "temp1", "Tctl")],
    )
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-box", "pci0000:00_0000:00:18_3", "temp1", 92.6)],
        [],
        *HWMON_ARGS,
        names,
    )
    assert limits[0][0] == "daniel-box k10temp/Tctl"


def test_an_unnamed_sensor_keeps_its_sysfs_identity():
    """The rejecting half: a partial name map must cost readability, never identity.

    Only 10 of the 21 live series carry a sensor label, so a chip whose name resolves and whose
    sensor does not is the common case, not an edge one — and the raw component is what a
    Prometheus query is written against.
    """
    names = checks_host.hwmon_name_maps(
        [_chip_name("daniel-box", "thermal_thermal_zone0", "acpitz")], []
    )
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-box", "thermal_thermal_zone0", "temp0", 20.0)],
        [],
        *HWMON_ARGS,
        names,
    )
    assert limits[0][0] == "daniel-box acpitz/temp0"

    unnamed = checks_host.hwmon_temp_limits(
        [_temp("daniel-box", "thermal_thermal_zone0", "temp0", 20.0)],
        [],
        *HWMON_ARGS,
        checks_host.hwmon_name_maps([], []),
    )
    assert unnamed[0][0] == "daniel-box thermal_thermal_zone0/temp0"


def test_an_estate_wide_breach_counts_the_sensors_it_does_not_list():
    """A truncated list must say so — a dropped tail reads as a smaller fault than it is."""
    limits = checks_host.hwmon_temp_limits(
        [_temp("daniel-box", "chip%d" % i, "temp0", 99.0) for i in range(8)],
        [],
        *HWMON_ARGS,
    )
    ok, msg = checks_host.hwmon_temp_verdict(limits)
    assert not ok
    assert msg.startswith("8 of 8 sensors over limit:")
    assert "+3 more" in msg, msg


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
    limits = checks_host.hwmon_temp_limits(temps, maxes, *HWMON_ARGS)
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
    ok, msg = checks_host.hwmon_temp_verdict([])
    assert not ok
    assert "collector blind" in msg


def test_a_single_spike_is_held_and_sustained_heat_pages(monkeypatch):
    """Hysteresis, both halves: one hot cycle must not page, the Nth must."""
    bridge_streaks._down_streaks.pop("host_temp", None)
    _stub_prom(
        monkeypatch, [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 99.0)]
    )
    results = [
        checks_host.check_host_temp()
        for _ in range(bridge_config.HWMON_TEMP_CONSECUTIVE)
    ]
    assert all(ok for ok, _msg in results[:-1]), (
        "a one-cycle thermal spike must not page"
    )
    assert not results[-1][0], "sustained heat must page on the Nth consecutive cycle"


def test_the_check_fetches_the_names_it_reports(monkeypatch):
    """The pure tests are handed a name map; only this one proves check_host_temp builds one.

    The two name metrics are separate queries, so a wiring that forgets them still passes every
    verdict test above and ships the sysfs path to the Kuma tile.
    """
    bridge_streaks._down_streaks.pop("host_temp", None)
    _stub_prom(
        monkeypatch,
        [_temp("daniel-box", "pci0000:00_0000:00:18_3", "temp1", 92.6)],
        chip_names=[_chip_name("daniel-box", "pci0000:00_0000:00:18_3", "k10temp")],
        sensor_labels=[
            _sensor_label("daniel-box", "pci0000:00_0000:00:18_3", "temp1", "Tctl")
        ],
    )
    for _ in range(bridge_config.HWMON_TEMP_CONSECUTIVE):
        _ok, msg = checks_host.check_host_temp()
    assert "daniel-box k10temp/Tctl" in msg, msg
    # A single-host estate also advances the module-global coverage-shortfall streak, which
    # would otherwise make a later test's clean cycle page.
    checks_host._host_origin_streaks.clear()


def test_a_clean_cycle_clears_the_streak(monkeypatch):
    bridge_streaks._down_streaks["host_temp"] = 2
    _stub_prom(
        monkeypatch, [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 40.0)]
    )
    ok, _msg = checks_host.check_host_temp()
    assert ok
    assert bridge_streaks._down_streaks["host_temp"] == 0, (
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


# ── host-coverage floor (HWMON_TEMP_ORIGINS_MIN) ──────────────────────────────────────────────
# THE GAP THESE PIN (2026-08-29 review M-9): hwmon_temp_verdict pages only on a WHOLLY empty
# list, so any non-empty subset passed. Lose one host's hwmon collector — node-exporter still
# up, which is its documented normal failure mode — and the surviving hosts answered "all below
# limit" for the whole estate. check_cluster_targets catches a TOTAL node outage; nothing caught
# the partial one, which is the shape of the 2026-08-23 incident that produced HOST_ORIGINS_MIN.
#
# The refuted first fix is worth naming: it added an env key nothing read, leaving hwmon on the
# shared floor of 2, so two of three hosts still satisfied it. Every test here therefore drives
# check_host_temp() rather than asserting a constant.

ALL_THREE = ("daniel-server", "daniel-box", "daniel-pi")


def _origin_temp(origin, chip, sensor, value):
    """One prom_vector element carrying the `origin` label the coverage floor counts."""
    return (
        {"origin": origin, "instance": origin, "chip": chip, "sensor": sensor},
        value,
    )


def _cool_estate(origins):
    """A cool, non-excluded sensor for each named host."""
    return [
        _origin_temp(o, "thermal_thermal_zone0", "temp0", 40.0) for o in sorted(origins)
    ]


def _reset():
    checks_host._host_origin_streaks.clear()
    bridge_streaks._down_streaks.pop("host_temp", None)


def test_full_coverage_is_clean(monkeypatch):
    _reset()
    _stub_prom(monkeypatch, _cool_estate(ALL_THREE))
    ok, msg = checks_host.check_host_temp()
    assert ok
    assert "hosts reporting" not in msg, (
        "full coverage must not carry a shortfall complaint"
    )


def test_a_missing_host_pages_once_the_grace_expires(monkeypatch):
    """The rejecting half. Two of three hosts is exactly the state the shared floor of 2 met."""
    _reset()
    _stub_prom(monkeypatch, _cool_estate(("daniel-server", "daniel-box")))
    results = [
        checks_host.check_host_temp()
        for _ in range(bridge_config.HWMON_TEMP_ORIGINS_CONSECUTIVE)
    ]
    assert not results[-1][0], "a host absent for the whole grace must page"
    msg = results[-1][1]
    assert "2 of 3" in msg and "NOT being checked" in msg, msg


def test_a_short_coverage_gap_is_held(monkeypatch):
    """The accepting half:

    the Pi's hwmon series went absent for about 20 minutes over the 7d to 2026-08-29, so a floor
    with no grace would page on a healthy estate.
    """
    _reset()
    _stub_prom(monkeypatch, _cool_estate(("daniel-server", "daniel-box")))
    held = [
        checks_host.check_host_temp()
        for _ in range(bridge_config.HWMON_TEMP_ORIGINS_CONSECUTIVE - 1)
    ]
    assert all(ok for ok, _msg in held), "a brief gap must not page"
    assert "cycle" in held[-1][1], "a held gap must still say what it is holding"


def test_full_coverage_clears_the_shortfall_streak(monkeypatch):
    _reset()
    _stub_prom(monkeypatch, _cool_estate(("daniel-server", "daniel-box")))
    checks_host.check_host_temp()
    _stub_prom(monkeypatch, _cool_estate(ALL_THREE))
    ok, _msg = checks_host.check_host_temp()
    assert ok
    assert checks_host._host_origin_streaks["host_temp"] == 0, (
        "a full-coverage cycle must reset the shortfall streak"
    )


def test_a_hot_sensor_outranks_a_coverage_shortfall(monkeypatch):
    """Precedence, mirroring check_disk:

    a host that IS reporting and IS too hot pages ahead of a complaint about the absent one.
    Reporting the shortfall first would bury a real breach.
    """
    _reset()
    _stub_prom(
        monkeypatch,
        [
            _origin_temp("daniel-server", "thermal_thermal_zone0", "temp0", 99.0),
            _origin_temp("daniel-box", "thermal_thermal_zone0", "temp0", 40.0),
        ],
    )
    for _ in range(bridge_config.HWMON_TEMP_CONSECUTIVE):
        ok, msg = checks_host.check_host_temp()
    assert not ok
    assert "over limit" in msg, msg
    assert "hosts reporting" not in msg, (
        "the breach message must not be replaced by the coverage complaint"
    )


def test_the_two_graces_are_not_compounded(monkeypatch):
    """A missing host must page within its OWN grace, not that grace times the thermal one.

    down_streak is the thermal-spike grace. Routing the shortfall's failing verdict through it
    as well would take a missing host from 25 minutes to 75 before anything fired, which is the
    kind of delay that reads as coverage right up until it matters.
    """
    _reset()
    _stub_prom(monkeypatch, _cool_estate(("daniel-server", "daniel-box")))
    fired = None
    for i in range(1, bridge_config.HWMON_TEMP_ORIGINS_CONSECUTIVE * 3 + 1):
        if not checks_host.check_host_temp()[0] and fired is None:
            fired = i
    assert fired == bridge_config.HWMON_TEMP_ORIGINS_CONSECUTIVE, (
        "the shortfall must page on its own Nth cycle, with no second grace stacked on it"
    )


def test_a_host_whose_only_sensors_are_excluded_does_not_count(monkeypatch):
    """The shared-predicate guard.

    HWMON_TEMP_EXCLUDE_CHIP drops the nvme chips, so a host that scrapes nothing else is a host this
    check does not cover — counting it toward the floor would satisfy the coverage requirement with
    a host nothing is watching.
    """
    _reset()
    _stub_prom(
        monkeypatch,
        _cool_estate(("daniel-server", "daniel-box"))
        + [_origin_temp("daniel-pi", "nvme_nvme0", "temp1", 40.0)],
    )
    results = [
        checks_host.check_host_temp()
        for _ in range(bridge_config.HWMON_TEMP_ORIGINS_CONSECUTIVE)
    ]
    assert not results[-1][0], (
        "an all-excluded host must not satisfy the floor; if it does, the origin count is "
        "reading the raw vector rather than the series the check actually covers"
    )


def test_the_floor_and_its_grace_are_pinned_and_overridable():
    """Pins the shipped values and their env keys together — the refuted M-9 fix was a key
    nothing read, which is indistinguishable from this test's absence."""
    assert bridge_config.HWMON_TEMP_ORIGINS_MIN == 3, (
        "3, not the shared HOST_ORIGINS_MIN of 2: all three hosts declare non-excluded hwmon "
        "sensors (measured 2026-08-29: 9 / 5 / 2), so a floor of 2 is met by any two of them"
    )
    assert bridge_config.HOST_ORIGINS_MIN == 2, (
        "the shared floor must stay 2 — disk and memory depend on it and test_check_host pins it"
    )
    assert (
        bridge_config.HWMON_TEMP_ORIGINS_CONSECUTIVE
        > bridge_config.HOST_ORIGINS_CONSECUTIVE
    ), (
        "the Pi drops out for longer than either amd64 node: about 20 min observed over the 7d "
        "to 2026-08-29, against 15 min of the shared grace at INTERVAL=300"
    )
    env_secret = (
        Path(check.__file__).resolve().parents[1] / "templates" / "env-secret.yaml.j2"
    ).read_text()
    for key, value in (
        ("HWMON_TEMP_ORIGINS_MIN", "3"),
        ("HWMON_TEMP_ORIGINS_CONSECUTIVE", "5"),
    ):
        assert '%s: "%s"' % (key, value) in env_secret, (
            "%s must be rendered so an operator can stand the arm down for a planned "
            "single-host maintenance window rather than editing check.py" % key
        )


def test_a_dead_node_exporter_suppresses_this_check():
    """With the floor armed, a dead node-exporter drops that host's series and trips it.

    Without this entry one root cause pages twice — Scrape Targets plus a coverage complaint.
    """
    assert "host_temp" in check.EXPORTER_DEPENDENT["node"]
    assert check.down_exporters([({"job": "node"}, 0)]) == {"node"}


@pytest.mark.parametrize("job", ["node", "node-pi"])
def test_every_job_carrying_hwmon_series_suppresses_this_check(job):
    """The reject half of the test above: a `node`-only map leaves the Pi double-paging.

    daniel-pi scrapes under job=node-pi (measured 2026-08-29), so its exporter death drops the
    two hwmon origins the floor counts while Scrape Targets pages for the same fact. Asserting
    only the `node` key passes with the Pi's gap wide open, which is how it was missed.
    """
    suppressed = set()
    for down in check.down_exporters([({"job": job}, 0)]):
        suppressed |= check.EXPORTER_DEPENDENT[down]
    assert "host_temp" in suppressed
