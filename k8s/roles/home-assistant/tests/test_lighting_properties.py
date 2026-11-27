"""Properties of the wake ramp and the ambient-fill brightness in custom_templates/lighting.jinja.

Run: uv run pytest k8s/roles/home-assistant/tests/test_lighting_properties.py
"""

from hypothesis import given
from hypothesis import strategies as st
from jinja_harness import render_macro

LIGHT = "lighting.jinja"

ramp_minutes = st.floats(0, 45, allow_nan=False)


def brightness(elapsed):
    return int(render_macro(LIGHT, "wake_brightness", elapsed))


@given(a=ramp_minutes, b=ramp_minutes)
def test_the_ramp_only_brightens_and_stays_in_range(a, b):
    early, late = sorted((a, b))
    assert 1 <= brightness(early) <= brightness(late) <= 100


@given(e=st.floats(0, 44.9, allow_nan=False))
def test_the_ramp_rises_at_most_one_percent_per_six_seconds(e):
    assert brightness(e + 0.1) - brightness(e) <= 1


@given(
    hour=st.integers(0, 23),
    lux=st.floats(0, 2000, allow_nan=False),
    more=st.floats(0, 2000, allow_nan=False),
)
def test_ambient_fill_dims_as_the_room_brightens_and_stays_visible(hour, lux, more):
    dim = int(render_macro(LIGHT, "natural_brightness", hour, lux))
    dimmer = int(render_macro(LIGHT, "natural_brightness", hour, lux + more))
    assert 5 <= dimmer <= dim <= 55
