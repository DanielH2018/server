"""Tests for the git-derived rotation dates in scripts/secrets_mgmt/git_dates.py.

Every test drives a synthetic history through `RotationTools.git`, so nothing here reads the
real repository or the encrypted store. The two halves that matter are the derivation itself
(a value changed, versus a file merely reordered) and `advance_last_rotated`'s advance-only
rule, which is what stops the derivation ever CREATING an overdue secret.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_git_dates.py
"""

import datetime as dt
import subprocess

import pytest

from _rotation_fakes import Fakes, build_tools
from secrets_mgmt.git_dates import (
    advance_last_rotated,
    ciphertext_rotation_dates,
    derived_rotation_dates,
)


def _history_tools(revs):
    """A `RotationTools` whose git reads `revs`: newest-first (sha, "YYYY-MM-DD", blob)."""
    return build_tools(Fakes(history=revs))[0]


def test_derived_date_is_the_commit_that_changed_the_value():
    tools = _history_tools(
        [
            ("c", "2026-08-01", {"tok": "ENC[new]"}),
            ("b", "2026-05-01", {"tok": "ENC[old]"}),
            ("a", "2026-01-01", {"tok": "ENC[old]"}),
        ]
    )
    assert ciphertext_rotation_dates(tools)["tok"] == dt.date(2026, 8, 1)


def test_unchanged_value_dates_to_the_oldest_revision():
    tools = _history_tools(
        [
            ("b", "2026-08-01", {"tok": "ENC[same]"}),
            ("a", "2026-01-01", {"tok": "ENC[same]"}),
        ]
    )
    assert ciphertext_rotation_dates(tools)["tok"] == dt.date(2026, 1, 1)


def test_reordering_does_not_count_as_a_rotation():
    """A regroup rewrites most of the file's lines while changing no value.

    Comparing the parsed value per key is what stops that marking every secret freshly rotated.
    """
    tools = _history_tools(
        [
            ("b", "2026-08-01", {"b_tok": "ENC[b]", "a_tok": "ENC[a]"}),
            ("a", "2026-01-01", {"a_tok": "ENC[a]", "b_tok": "ENC[b]"}),
        ]
    )
    dates = ciphertext_rotation_dates(tools)
    assert dates["a_tok"] == dt.date(2026, 1, 1)
    assert dates["b_tok"] == dt.date(2026, 1, 1)


def test_advance_moves_a_stale_date_forward():
    reg = {"entries": {"tok": {"tier": "assisted", "last_rotated": "2025-08-24"}}}
    advanced = advance_last_rotated(reg, {"tok": dt.date(2026, 3, 13)})
    assert advanced == [("tok", "2025-08-24", "2026-03-13")]
    assert reg["entries"]["tok"]["last_rotated"] == "2026-03-13"


def test_advance_never_moves_a_date_backward():
    """Advance-only is what stops this creating an overdue secret.

    A registry date newer than git's — a rotation recorded before its commit landed — must survive.
    """
    reg = {"entries": {"tok": {"tier": "assisted", "last_rotated": "2026-08-25"}}}
    assert advance_last_rotated(reg, {"tok": dt.date(2026, 3, 13)}) == []
    assert reg["entries"]["tok"]["last_rotated"] == "2026-08-25"


def test_advance_ignores_secrets_git_has_no_date_for():
    reg = {"entries": {"tok": {"tier": "assisted", "last_rotated": "2025-08-24"}}}
    assert advance_last_rotated(reg, {}) == []
    assert reg["entries"]["tok"]["last_rotated"] == "2025-08-24"


def test_derivation_failure_degrades_to_recorded_dates():
    """A cron that cannot read git must fall back, not fail — a broken derivation taking
    the monitor down would be a worse outage than the drift it corrects."""
    tools = build_tools(Fakes(git_error=subprocess.CalledProcessError(128, "git")))[0]
    assert derived_rotation_dates(tools) == {}


# --- the fake's failure shape ----------------------------------------------------------------


def test_the_git_fake_names_an_unscripted_ref_with_its_argv():
    """The red-proof half: the blob lookup used to raise a bare `KeyError` naming the sha.

    A `KeyError('deadbeef')` reads as "that sha is not in this history" — a plausible answer —
    where the sibling fakes (`_findings_fakes.py`, `_land_fakes.py`, `_deploy_fakes.py`) raise a
    named `AssertionError` carrying the argv, which reads as "this call was never scripted".
    """
    tools = build_tools(Fakes(history=[]))[0]
    with pytest.raises(AssertionError) as caught:
        tools.git("show", "deadbeef:ansible/vars/secrets.yml")
    assert "unscripted git call" in str(caught.value)
    assert "deadbeef" in str(caught.value)


def test_the_git_fake_rejects_an_unscripted_verb():
    """An unscripted VERB fell through to the blob lookup and raised the same bare KeyError.

    The ref is one the history DOES carry, so only the verb clause of the guard can fire —
    otherwise this test and the one above would prove the same branch twice.
    """
    history = [("deadbeef", "2026-01-01", {"a_token": "x"})]
    tools = build_tools(Fakes(history=history))[0]
    with pytest.raises(AssertionError) as caught:
        tools.git("rev-parse", "deadbeef:ansible/vars/secrets.yml")
    assert "rev-parse" in str(caught.value)
