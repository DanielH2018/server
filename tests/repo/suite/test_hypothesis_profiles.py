"""The Hypothesis profiles `tests/hypothesis_profiles.py` registers, and which one a run loads.

Run: uv run pytest tests/repo/suite/test_hypothesis_profiles.py
"""

from hypothesis import settings


def test_ci_replays_fixed_inputs_and_sweep_draws_fresh_ones_and_more_of_them():
    ci, sweep = settings.get_profile("ci"), settings.get_profile("sweep")

    assert (ci.derandomize, ci.database) == (True, None)
    assert sweep.derandomize is False
    assert sweep.max_examples > ci.max_examples


def test_a_run_loads_the_profile_its_flag_names_and_ci_without_one(request):
    named = request.config.getoption("hypothesis_profile")

    assert settings.get_current_profile_name() == (named or "ci")
