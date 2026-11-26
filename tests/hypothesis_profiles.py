"""The Hypothesis profiles every property test runs under, loaded as a plugin from `addopts`.

`ci` is the default. It fixes the seed and keeps no example database, so a CI run draws the
same inputs as a local run and a red pull request reproduces anywhere. That also means it draws
the same inputs every run, so it finds nothing new once a property has passed.

`sweep` draws fresh inputs and ten times as many. The nightly `.github/workflows/pytest-sweep.yml`
selects it with `--hypothesis-profile=sweep`. A failure there prints a `@reproduce_failure`
blob: pin the failing input as an `@example` on the test, and `ci` draws it on every run.

A test module states neither a seed policy nor an example count of its own. The
`property-tests-take-their-settings-from-the-profile` row of
`scripts/tests/test_census_rows_test_modules.py` refuses a `derandomize=` outside this file.

Run: uv run pytest --hypothesis-profile=sweep <path>
"""

from hypothesis import settings

CI_EXAMPLES = 300

settings.register_profile(
    "ci", derandomize=True, database=None, deadline=None, max_examples=CI_EXAMPLES
)
settings.register_profile(
    "sweep",
    derandomize=False,
    database=None,
    deadline=None,
    max_examples=10 * CI_EXAMPLES,
    print_blob=True,
)
settings.load_profile("ci")
