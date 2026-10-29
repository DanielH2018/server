"""Every key secrets.yml has ever held and holds no longer is a recorded rename or a listed retirement.

A SOPS rename re-encrypts the value, so the rename commit reads as a rotation unless the new
name is in `RENAMED_FROM` (`git_dates.py`). Nothing checked that table was complete: a
rename that skipped it reset the secret's clock silently, and the audit reported the
credential fresh. This walks the store's real git history — key NAMES only, the raw blob of
each revision, never a decrypted diff — and holds the departed set to `RENAMED_FROM` plus
`RETIRED`. The other direction too: a name a table calls gone must not be back in the store.

The synthetic clean/flagged pairs for both checks are in `test_secret_git_dates.py`; this
file is the one that reads the repository. It depends on the pytest job's `fetch-depth: 0`
the same way `test_module_length_ratchet.py` does (`test_ci_pytest_job_fetch_depth.py`
names that dependency): a shallow checkout would hand it a truncated history, which is
why the non-vacuity assertion below names members the history must contain.

Run: uv run pytest scripts/secrets_mgmt/tests/test_departed_secrets_are_accounted_for.py
"""

import pytest

from secrets_mgmt.git_dates import (
    RENAMED_FROM,
    RETIRED,
    historical_names,
    revived_departures,
    undeclared_departures,
)
from secrets_mgmt.pending_retirement import PENDING_RETIREMENT
from secrets_mgmt.rotation_tools import RotationTools


@pytest.fixture(scope="module")
def history() -> set[str]:
    return historical_names(RotationTools())


@pytest.fixture(scope="module")
def current() -> set[str]:
    return set(RotationTools().sops_names())


def test_the_history_read_is_not_vacuous(history):
    """A truncated or empty history would pass the departure check with nothing to check.

    Every `RENAMED_FROM` source and the first-retired name are real departures; a walk that
    cannot find them read the wrong history.
    """
    assert "beszel_agent_key" in RETIRED
    anchors = set(RENAMED_FROM.values()) | {"beszel_agent_key"}
    assert anchors <= history, sorted(anchors - history)


def test_every_departed_key_is_a_recorded_rename_or_a_listed_retirement(
    history, current
):
    gaps = undeclared_departures(history, current)
    assert not gaps, (
        "keys the store once held and holds no longer, in neither RENAMED_FROM nor RETIRED "
        "(scripts/secrets_mgmt/git_dates.py) — a rename needs a RENAMED_FROM entry and its "
        "last_rotated carried over with `secret_rotation.py record`; a retirement is listed "
        "in RETIRED: %s" % sorted(gaps)
    )


def test_no_listed_departure_is_back_in_the_store(current):
    revived = revived_departures(current)
    assert not revived, "listed as gone but present in secrets.yml: %s" % sorted(
        revived
    )


def test_a_pending_retirement_is_still_in_the_store_and_not_yet_retired(current):
    """The allowance ends with the keys: a name that left `secrets.yml` must leave the set too.

    Named members, so an emptied set cannot pass. A name in both tables would say a key is
    pending and gone at once.
    """
    assert "daniel_box_disk_push_token" in PENDING_RETIREMENT
    assert PENDING_RETIREMENT <= current, sorted(PENDING_RETIREMENT - current)
    assert not PENDING_RETIREMENT & RETIRED
