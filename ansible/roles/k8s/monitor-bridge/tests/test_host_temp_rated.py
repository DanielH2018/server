"""The rated arm of check_host_temp: a published Tjmax for a sensor whose driver declares none.

Split from `test_host_temp.py`, which is at its module-length cap. The arm exists because
daniel-box's k10temp publishes neither `temp1_max` nor `temp1_crit` for Tctl — the CPU's only
temperature reading — so before `HWMON_TEMP_RATED_MAX_C` it took a flat 85C chosen for the
estate rather than for a Ryzen 7 8845HS (#1152, and the `DECIDED:` markers in verdicts.host).

The last two tests are the ones that carry the arm. Every test above them hands `rated` in
directly and would keep passing if the deployed template shipped nothing at all; those two read
the value the pod is actually given, through the real parser.
"""

from pathlib import Path

import bridge.config_host
import checks.host_thermal

# The role directory, for the template these tests read back. `tests/` is its sibling.
_ROLE = Path(__file__).resolve().parents[1]


def _temp(instance, chip, sensor, value):
    """One prom_vector element, in node-exporter's hwmon label shape."""
    return ({"instance": instance, "chip": chip, "sensor": sensor}, value)


# ratio, fallback_c, min_plausible, max_plausible, exclude_chip — the deployed values.
HWMON_ARGS = (0.90, 85.0, 20.0, 150.0, "nvme_")

# daniel-box's k10temp/Tctl, as node-exporter labels it.
_BOX_K10TEMP = ("daniel-box", "pci0000:00_0000:00:18_3", "temp1")
# AMD's published `Max. Operating Temperature (Tjmax)` for the Ryzen 7 8845HS.
_RATED_8845HS = [(_BOX_K10TEMP, 100.0)]


def _host_config(raw):
    """The real host_config over a stub environment, so the shipped parser is what runs."""
    problems = []

    def _env(name, default="", *_args, **_kwargs):
        return raw if name == "HWMON_TEMP_RATED_MAX_C" else default

    def _int(_name, default="0", *_args, **_kwargs):
        return int(default)

    def _num(_name, default="0", *_args, **_kwargs):
        return float(default)

    return bridge.config_host.host_config(_env, _int, _num, _env, problems), problems


def test_a_rated_sensor_is_clean_below_the_ratio():
    """The rated arm ratios like a declared max: 100C rated pages at 90C, not at 85C.

    88.0C is the case the flat fallback got wrong — over 85 and therefore paging, but inside
    what AMD rates this part for. It must read green, and by the "declared" basis, because the
    limit now comes from a stated rating for this part rather than from an estate-wide guess.
    """
    limits = checks.host_thermal.hwmon_temp_limits(
        [_temp(*_BOX_K10TEMP, 88.0)], [], *HWMON_ARGS, rated=_RATED_8845HS
    )
    assert limits[0][2:] == (90.0, "declared")
    ok, msg = checks.host_thermal.hwmon_temp_verdict(limits)
    assert ok, msg


def test_a_rated_sensor_is_flagged_above_the_ratio():
    """The rejecting half: recalibration must narrow the alert, never remove it.

    93.75C is the highest reading Prometheus holds for this sensor in the 30 days to
    2026-09-05. It breached the old 85C fallback and it must still breach the rated 90C — the
    part runs genuinely close to its rating, and a limit derived from that rating has to fire.
    """
    limits = checks.host_thermal.hwmon_temp_limits(
        [_temp(*_BOX_K10TEMP, 93.75)], [], *HWMON_ARGS, rated=_RATED_8845HS
    )
    ok, msg = checks.host_thermal.hwmon_temp_verdict(limits)
    assert not ok
    assert "over its 90.0C declared limit" in msg


def test_an_unrated_sensor_still_takes_the_flat_fallback():
    """The rated arm is per-sensor and must not leak: one entry does not recalibrate the estate.

    daniel-pi has no fan and declares no max, so it is the sensor that most needs the flat
    ceiling to stay exactly where it was while another host's sensor moves.
    """
    limits = checks.host_thermal.hwmon_temp_limits(
        [_temp("daniel-pi", "thermal_thermal_zone0", "temp0", 86.0)],
        [],
        *HWMON_ARGS,
        rated=_RATED_8845HS,
    )
    assert limits[0][2:] == (85.0, "fallback")
    ok, _msg = checks.host_thermal.hwmon_temp_verdict(limits)
    assert not ok, "the fallback arm must still fire for a sensor no rating covers"


def test_a_declared_max_beats_a_rating():
    """Precedence: the hardware's own word beats a constant an operator typed.

    If the driver starts publishing a max for this chip, that max is the better source and must
    win. Here it declares 80, so the limit is 72 and not the 90 the rating alone would give.
    """
    limits = checks.host_thermal.hwmon_temp_limits(
        [_temp(*_BOX_K10TEMP, 75.0)],
        [_temp(*_BOX_K10TEMP, 80.0)],
        *HWMON_ARGS,
        rated=_RATED_8845HS,
    )
    assert limits[0][2:] == (72.0, "declared")
    ok, _msg = checks.host_thermal.hwmon_temp_verdict(limits)
    assert not ok, (
        "the declared max must be the one that decides, and must be able to fire"
    )


def test_an_implausible_rating_falls_through_to_the_fallback():
    """A typo'd rating must not un-watch the sensor — the sentinel lesson, applied to config.

    1000C ratioed is 900C, which nothing reaches. The same plausibility gate that rejects the
    65261.85 NVMe sentinel therefore applies to a rated value, and the sensor drops to the flat
    ceiling rather than to no effective limit at all.
    """
    limits = checks.host_thermal.hwmon_temp_limits(
        [_temp(*_BOX_K10TEMP, 93.75)], [], *HWMON_ARGS, rated=[(_BOX_K10TEMP, 1000.0)]
    )
    assert limits[0][2:] == (85.0, "fallback")
    ok, _msg = checks.host_thermal.hwmon_temp_verdict(limits)
    assert not ok, "a rejected rating must leave the sensor watched, not unwatched"


def test_the_shipped_config_rates_daniel_boxs_k10temp():
    """Non-vacuity: the deployed template must carry the entry, in a shape the parser reads.

    Every test above hands `rated` in directly, so all of them would still pass if the template
    shipped nothing, or shipped a key the parser cannot read — the sensor would quietly return
    to the flat 85 in production with the suite green. This is the only test that reads the
    value the pod is given, and it runs it through the REAL parser rather than a regex, so a
    separator that disagrees between template and parser fails here.
    """
    declared = [
        line
        for line in (_ROLE / "templates/env-secret.yaml.j2").read_text().splitlines()
        if line.strip().startswith("HWMON_TEMP_RATED_MAX_C:")
    ]
    assert len(declared) == 1, (
        "env-secret.yaml.j2 must declare HWMON_TEMP_RATED_MAX_C exactly once, found %d"
        % len(declared)
    )
    cfg, problems = _host_config(declared[0].split(":", 1)[1].strip().strip('"'))
    assert not problems, problems
    assert dict(cfg.HWMON_TEMP_RATED_MAX_C).get(_BOX_K10TEMP) == 100.0, (
        "daniel-box's k10temp/Tctl must ship AMD's published Tjmax of 100C for the Ryzen 7 "
        "8845HS; got %r. If the sensor's sysfs triple changed, update the key — do not drop "
        "the entry, or the CPU silently returns to the uncalibrated 85C fallback."
        % (cfg.HWMON_TEMP_RATED_MAX_C,)
    )


def test_a_malformed_rating_is_reported_rather_than_guessed_at():
    """The rejecting half of the parser: a bad entry names itself in `problems`.

    Silently dropping it would leave the sensor on the fallback with nothing saying why, which
    is the same failure as the entry never having been written.
    """
    cfg, problems = _host_config("daniel-box/onlytwo=100")
    assert cfg.HWMON_TEMP_RATED_MAX_C == ()
    assert any("HWMON_TEMP_RATED_MAX_C" in p for p in problems), problems


def test_the_shipped_hysteresis_rides_out_this_sensors_boost_excursions():
    """#1186: the same sensor's other half — the streak length, pinned to what was measured.

    The rated arm above settles WHERE the limit is; this settles HOW LONG a breach must last to
    page. Both are daniel-box's k10temp/Tctl, which spends 12.0% of a week above that 90C limit
    as ordinary boost. Measured over the 7d to 2026-09-06 at the 5 min loop cadence, its 115
    excursions ran to 8 cycles with one outlier of 18 and nothing in between; 3 cycles paged 26
    times that week, 12 pages once. The env-secret value is the one the pod reads, so a code-only
    change ships nothing — both are pinned here.
    """
    cfg, problems = _host_config("")
    assert not problems, problems
    assert cfg.HWMON_TEMP_CONSECUTIVE == 12, (
        "12 cycles = 60 min at INTERVAL=300, from the empty 9-17 cycle gap in the measured "
        "run-length distribution. Shortening it reinstates the 26-pages-a-week condition #1186 "
        "closed; the derivation is at the `DECIDED: 12 cycles` marker in bridge/config_host.py"
    )
    assert (
        'HWMON_TEMP_CONSECUTIVE: "12"'
        in (_ROLE / "templates/env-secret.yaml.j2").read_text()
    ), "the env-secret overrides the code default, so it must carry the same 12"
