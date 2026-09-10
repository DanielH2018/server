"""The two arms check_host_temp gained for issue #1471: undervoltage, and CPU thermal throttling.

Both signals were plotted on Infrastructure/hardware-thermal.json (PR #1463) and alerted on by
nothing. Every rule below is a PAIR — one input it must accept, one it must reject — because a
thermal arm's failure mode is silence, not a wrong number, and a rule that fires on nothing
passes its accepting half exactly as a working rule does.

The one that matters most is `test_an_empty_undervoltage_vector_pages_when_the_pi_is_scraping`.
`node_hwmon_in_lcrit_alarm_volts` is one series from one host, so a `max() > 0` arm reads GREEN
the moment daniel-pi stops answering — and a Pi falling off the network is what sustained
undervoltage causes. That is the false-GREEN this arm exists to avoid, and it is invisible to
every other test here.

Nothing here patches a first-party module. Both arms take their fetched vector and their
source-gate answer as arguments, so a test hands them inputs directly; `check_host_temp` owns
the two fetches. The cap in `ansible/tests/repo/test_module_length_ratchet.py` is what forced
that shape, and the shape is better for it — the arms hold decisions and streak state only.
"""

import dataclasses

import bridge.streaks
import checks.host_thermal
from verdicts.host_power import thermal_throttle_verdict, undervoltage_verdict


def _alarm(value, instance="daniel-pi", sensor="in0"):
    """One node_hwmon_in_lcrit_alarm_volts element, in the live label shape."""
    return (
        {
            "instance": instance,
            "chip": "soc:firmware_raspberrypi_hwmon",
            "sensor": sensor,
            "origin": instance,
        },
        value,
    )


def _cooling(origin, value, name="0"):
    """One node_cooling_device_cur_state{type="Processor"} element."""
    return (
        {"origin": origin, "instance": origin, "type": "Processor", "name": name},
        value,
    )


# ── undervoltage: the pure verdict ────────────────────────────────────────────────────────────


def test_an_asserted_undervoltage_alarm_is_flagged():
    ok, msg = undervoltage_verdict([_alarm(1.0)])
    assert not ok
    assert "undervoltage alarm asserted" in msg
    assert "daniel-pi" in msg


def test_a_quiet_undervoltage_alarm_is_clean():
    """The accepting half. A rule that flagged every reading would pass the test above too."""
    ok, msg = undervoltage_verdict([_alarm(0.0)])
    assert ok
    assert "no undervoltage alarm" in msg


def test_an_empty_undervoltage_vector_is_flagged_not_clean():
    """Zero readings is not "no alarm" — it is the sensor's only host having gone quiet."""
    ok, msg = undervoltage_verdict([])
    assert not ok
    assert "no undervoltage alarm sensor scraped" in msg


def test_the_undervoltage_message_names_the_sensor_by_its_readable_name():
    """The chip/sensor name maps are shared with the temperature arm, so they apply here too."""
    names = ({("daniel-pi", "soc:firmware_raspberrypi_hwmon"): "rpi_volt"}, {})
    ok, msg = undervoltage_verdict([_alarm(1.0)], names)
    assert not ok
    assert "daniel-pi rpi_volt/in0" in msg


# ── undervoltage: the arm, including the source gate ──────────────────────────────────────────

# The arm tests take conftest's `cfg` fixture — load_config({}), so every knob holds the
# documented default — rather than a hand-written stub class. A stub would let these tests keep
# passing against thresholds the deployed config no longer uses, which is the drift this repo
# treats as a green-but-wrong test. `dataclasses.replace` narrows one field where a test needs to.


def test_an_asserted_alarm_pages_on_the_first_cycle(cfg):
    """UNDERVOLTAGE_CONSECUTIVE is 1 deliberately: the firmware latched a bit, not a spike."""
    result = checks.host_thermal._undervoltage_arm(cfg, None, [_alarm(1.0)], True)
    assert result is not None
    ok, msg = result
    assert not ok
    assert "undervoltage alarm asserted" in msg


def test_a_quiet_arm_says_nothing(cfg):
    """None, not (True, msg): a clean arm must not change the monitor's ordinary tile text."""
    assert checks.host_thermal._undervoltage_arm(cfg, None, [_alarm(0.0)], True) is None


def test_an_empty_vector_defers_while_the_pi_scrape_is_down(cfg):
    """check_cluster_targets owns a dead scrape; this arm must not double-page it."""
    assert checks.host_thermal._undervoltage_arm(cfg, None, [], False) is None


def test_an_empty_undervoltage_vector_pages_when_the_pi_is_scraping(cfg):
    """THE red proof for the false-GREEN this arm exists to prevent.

    The Pi is affirmatively up and the sensor is gone: renamed, or the hwmon collector went
    blind. A `max() > 0` arm answers "no undervoltage" here. This one pages.
    """
    result = checks.host_thermal._undervoltage_arm(cfg, None, [], True)
    assert result is not None
    ok, msg = result
    assert not ok
    assert "no undervoltage alarm sensor scraped" in msg


def test_the_arm_is_off_when_no_query_is_configured(cfg):
    """An unconfigured arm is silent even on an input that would otherwise page."""
    off = dataclasses.replace(cfg, UNDERVOLTAGE_QUERY="")
    assert checks.host_thermal._undervoltage_arm(off, None, [_alarm(1.0)], True) is None


# ── CPU thermal throttling: the pure verdict ──────────────────────────────────────────────────


def test_a_throttling_cpu_is_flagged():
    ok, msg = thermal_throttle_verdict(
        [_cooling("daniel-box", 2.0), _cooling("daniel-server", 0.0)], 2
    )
    assert not ok
    assert "thermally throttled on daniel-box" in msg
    assert "daniel-server" not in msg.split(";")[0]


def test_an_idle_cpu_is_clean():
    """The accepting half."""
    ok, msg = thermal_throttle_verdict(
        [_cooling("daniel-box", 0.0), _cooling("daniel-server", 0.0)], 2
    )
    assert ok
    assert "not throttling" in msg
    assert "2 host(s)" in msg


def test_an_empty_cooling_vector_is_flagged():
    ok, msg = thermal_throttle_verdict([], 2)
    assert not ok
    assert "no Processor cooling-device series scraped" in msg


def test_one_host_short_of_the_floor_is_flagged():
    """The arm that stops this being inert: one node blind, the other reporting "not throttling".

    Without it, losing daniel-server's collector leaves daniel-box answering for the estate.
    """
    ok, msg = thermal_throttle_verdict([_cooling("daniel-box", 0.0)], 2)
    assert not ok
    assert "only 1 of 2 expected host(s)" in msg
    assert "daniel-box" in msg


def test_a_throttling_host_outranks_a_coverage_shortfall():
    """Ordering, mirroring hwmon_temp_verdict: the live fault is the more actionable of the two."""
    ok, msg = thermal_throttle_verdict([_cooling("daniel-box", 3.0)], 2)
    assert not ok
    assert msg.startswith("CPU thermally throttled")


# ── CPU thermal throttling: the arm ───────────────────────────────────────────────────────────


_THROTTLING = [_cooling("daniel-box", 1.0), _cooling("daniel-server", 0.0)]
_QUIET = [_cooling("daniel-box", 0.0), _cooling("daniel-server", 0.0)]


def test_sustained_throttling_pages_and_a_single_cycle_is_held(cfg):
    """THERMAL_THROTTLE_CONSECUTIVE is 3: a burst throttles for a cycle, a cooling fault does not."""
    first = checks.host_thermal._thermal_throttle_arm(cfg, _THROTTLING, True)
    assert first is not None and first[0], "one cycle must be held inside the grace"
    assert "(throttle grace)" in first[1] and "1/3" in first[1]
    for _ in range(cfg.THERMAL_THROTTLE_CONSECUTIVE - 1):
        last = checks.host_thermal._thermal_throttle_arm(cfg, _THROTTLING, True)
    assert last is not None and not last[0], "the Nth straight cycle must page"


def test_a_clean_cycle_clears_the_throttle_streak(cfg):
    checks.host_thermal._thermal_throttle_arm(cfg, _THROTTLING, True)
    assert bridge.streaks._down_streaks["host_thermal_throttle"] == 1
    assert checks.host_thermal._thermal_throttle_arm(cfg, _QUIET, True) is None
    assert bridge.streaks._down_streaks["host_thermal_throttle"] == 0


def test_an_empty_cooling_vector_defers_while_the_node_scrape_is_down(cfg):
    """Both amd64 exporters gone is check_cluster_targets' fault to report, not this arm's."""
    assert checks.host_thermal._thermal_throttle_arm(cfg, [], False) is None


def test_an_empty_cooling_vector_pages_while_the_node_scrape_is_up(cfg):
    """The red proof for the throttle arm's own blindness case.

    node-exporter is answering and publishes no Processor cooling device at all: a driver or
    kernel change took the sensors away. Nothing else in the estate would notice.
    """
    for _ in range(cfg.THERMAL_THROTTLE_CONSECUTIVE):
        result = checks.host_thermal._thermal_throttle_arm(cfg, [], True)
    assert result is not None
    ok, msg = result
    assert not ok, "blindness must page once its own grace expires, not be held forever"
    assert "no Processor cooling-device series scraped" in msg


def test_the_two_new_arms_keep_separate_streak_keys(cfg):
    """The arms must not compound: three counters, three keys.

    check_host_temp's docstring states this and nothing else enforces it — a shared key would let
    an undervoltage blip and a throttle blip page together at half the intended threshold.
    """
    checks.host_thermal._undervoltage_arm(cfg, None, [_alarm(1.0)], True)
    checks.host_thermal._thermal_throttle_arm(cfg, _THROTTLING, True)
    assert set(bridge.streaks._down_streaks) == {
        "host_undervoltage",
        "host_thermal_throttle",
    }


# ── the arms are actually wired into the check, not just importable ───────────────────────────


def test_check_host_temp_calls_both_arms_and_undervoltage_first():
    """Binds the arms into the check. Testing them alone would pass even if check_host_temp
    never called them — the inert-check shape this repo has paid for twice.

    Read off the compiled function rather than driven end to end, because driving it means
    faking `bridge.net` and this module is held to zero first-party patches. The ORDER of the
    two names in `co_names` is the order their call sites appear, which is the property the
    check's docstring claims: undervoltage returns ahead of everything else.
    """
    names = list(checks.host_thermal.check_host_temp.__code__.co_names)
    assert "_undervoltage_arm" in names, "the undervoltage arm is never called"
    assert "_thermal_throttle_arm" in names, "the throttle arm is never called"
    assert names.index("_undervoltage_arm") < names.index("hwmon_temp_verdict"), (
        "undervoltage must be judged before the temperature verdict"
    )
    assert names.index("hwmon_temp_verdict") < names.index("_thermal_throttle_arm"), (
        "the throttle arm belongs after a clean temperature verdict"
    )
