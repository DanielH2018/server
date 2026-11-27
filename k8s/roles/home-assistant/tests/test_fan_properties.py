"""The bedroom fan loop over generated sequences of temperature, night, sleep and Tap Dial input.

Each apply runs the loop the live system runs: `fan_target_level` picks a level,
`level_to_pct` sends its %, the DREO integration ceils that % to a level, and `pct_to_level`
reads the reported % back as the level the next apply starts from.

Run: uv run pytest k8s/roles/home-assistant/tests/test_fan_properties.py
"""

import math

from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from jinja_harness import _forgiving_round, render_macro

FAN = "fan.jinja"
UNAVAILABLE = "unavailable"
NIGHT_CAP = 4
SLEEP_CEILING = 5
# Less than (0.7 - 0.5) * 1.3 degF: the gap between a level's rounding band and its deadband.
NOISE_F = 0.25

temperatures = st.one_of(st.floats(55, 100, allow_nan=False), st.just(UNAVAILABLE))
outdoor_temperatures = st.one_of(
    st.floats(-10, 110, allow_nan=False), st.just(UNAVAILABLE)
)


def sleep_floor(outdoor):
    ot = outdoor if isinstance(outdoor, float) else -999
    return 4 if ot >= 68 else (3 if ot >= 45 else 2)


def target(temp, level, night, sleep, outdoor):
    return int(
        render_macro(FAN, "fan_target_level", temp, level, night, sleep, outdoor)
    )


def device_round_trip(level):
    sent = int(render_macro(FAN, "level_to_pct", level))
    device_level = math.ceil(sent * 9 / 100)
    reported = _forgiving_round(device_level * 100 / 9)
    return int(render_macro(FAN, "pct_to_level", reported))


class FanLoop(RuleBasedStateMachine):
    """The fan's inputs and the level the device last reported."""

    def __init__(self):
        super().__init__()
        self.temp = UNAVAILABLE
        self.night = False
        self.sleep = False
        self.outdoor = UNAVAILABLE
        self.level = 0
        self.settled_at = None

    def apply(self):
        wanted = target(self.temp, self.level, self.night, self.sleep, self.outdoor)
        self.level = device_round_trip(wanted)
        ideal = (self.temp - 71) / 1.3 if isinstance(self.temp, float) else None
        on_curve = (
            ideal is not None and self.level > 0 and abs(ideal - self.level) <= 0.5
        )
        self.settled_at = self.temp if on_curve else None

    @initialize(temp=temperatures)
    def first_reading(self, temp):
        self.temp = temp
        self.apply()

    @rule(temp=temperatures)
    def temperature_reading(self, temp):
        self.temp = temp
        self.apply()

    @rule(night=st.booleans())
    def clock(self, night):
        self.night = night
        self.apply()

    @rule(sleep=st.booleans(), outdoor=outdoor_temperatures)
    def sleep_mode(self, sleep, outdoor):
        self.sleep = sleep
        self.outdoor = outdoor
        self.apply()

    @rule(delta=st.sampled_from([-1, 1]))
    def tap_dial(self, delta):
        self.level = int(render_macro(FAN, "fan_nudge_level", self.level, delta))
        self.apply()

    @precondition(lambda self: self.settled_at is not None)
    @rule(noise=st.floats(-NOISE_F, NOISE_F))
    def sensor_noise_holds_a_settled_level(self, noise):
        held = target(
            self.settled_at + noise, self.level, self.night, self.sleep, self.outdoor
        )
        assert held == self.level, (
            f"{noise:+.2f} degF of noise moved L{self.level} to L{held}"
        )

    @invariant()
    def caps_hold(self):
        assert 0 <= self.level <= 9
        if self.sleep:
            assert sleep_floor(self.outdoor) <= self.level <= SLEEP_CEILING
        elif self.night:
            assert self.level <= NIGHT_CAP
        if self.temp == UNAVAILABLE and not self.sleep:
            assert self.level == 0

    @invariant()
    def unchanged_inputs_keep_the_level(self):
        again = target(self.temp, self.level, self.night, self.sleep, self.outdoor)
        assert again == self.level, f"same inputs moved L{self.level} to L{again}"


TestFanLoop = FanLoop.TestCase
