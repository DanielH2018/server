"""A window opened on `ventilation_advice` is not told to close by `close_window_advice`.

Run: uv run pytest k8s/roles/home-assistant/tests/test_ventilation_properties.py
"""

from hypothesis import given
from hypothesis import strategies as st
from jinja_harness import render_macro

VENT = "ventilation.jinja"

temperatures = st.floats(-10, 110, allow_nan=False)
particulates = st.floats(0, 300, allow_nan=False)


@given(
    indoor=temperatures,
    outdoor=temperatures,
    indoor_pm=particulates,
    outdoor_pm=particulates,
    outdoor_pm10=particulates,
    stale=st.booleans(),
    pm_safe=st.floats(5, 60, allow_nan=False),
    pm10_safe=st.floats(20, 150, allow_nan=False),
)
def test_a_window_opened_on_advice_is_not_told_to_close(
    indoor, outdoor, indoor_pm, outdoor_pm, outdoor_pm10, stale, pm_safe, pm10_safe
):
    caps = {"outdoor_pm10": outdoor_pm10, "pm_safe": pm_safe, "pm10_safe": pm10_safe}
    opened = render_macro(
        VENT,
        "ventilation_advice",
        indoor,
        outdoor,
        indoor_pm,
        outdoor_pm,
        stale,
        **caps,
    )
    closed = render_macro(
        VENT, "close_window_advice", indoor, outdoor, outdoor_pm, False, **caps
    )
    assert opened == "none" or closed == "none", f"advised {opened}, then {closed}"
