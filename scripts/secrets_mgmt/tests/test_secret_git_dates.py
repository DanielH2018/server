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


def test_a_rename_is_not_a_rotation():
    """SOPS binds a value's ciphertext to its key path, so a rename re-encrypts it.

    Reject half of the `RENAMED_FROM` rule. Without it the rename commit dates the secret to
    itself, which advances `last_rotated` to the rename and overstates the credential's
    freshness by however long it had really been sitting there.
    """
    tools = _history_tools(
        [
            ("c", "2026-09-09", {"longhorn_b2_key_id": "ENC[rekeyed]"}),
            ("b", "2026-05-29", {"kopia_b2_key_id": "ENC[old]"}),
            ("a", "2026-01-01", {"kopia_b2_key_id": "ENC[older]"}),
        ]
    )
    assert "longhorn_b2_key_id" not in ciphertext_rotation_dates(tools)


def test_a_rotation_after_a_rename_is_still_derived():
    """Accept half: once both revisions spell the key the new way, the rule is out of the path."""
    tools = _history_tools(
        [
            ("d", "2026-11-02", {"longhorn_b2_key_id": "ENC[rotated]"}),
            ("c", "2026-09-09", {"longhorn_b2_key_id": "ENC[rekeyed]"}),
            ("b", "2026-05-29", {"kopia_b2_key_id": "ENC[old]"}),
        ]
    )
    assert ciphertext_rotation_dates(tools)["longhorn_b2_key_id"] == dt.date(
        2026, 11, 2
    )


def test_an_unrelated_new_secret_still_dates_to_its_introduction():
    """A key absent from the older revision and NOT a recorded rename dates to where it appeared.

    This is what stops `rotation_evidence` degrading into "absence is never evidence".
    """
    tools = _history_tools(
        [
            ("b", "2026-08-01", {"tok": "ENC[a]", "fresh_tok": "ENC[b]"}),
            ("a", "2026-01-01", {"tok": "ENC[a]"}),
        ]
    )
    assert ciphertext_rotation_dates(tools)["fresh_tok"] == dt.date(2026, 8, 1)


def _stable_keys(n: int, prefix: str = "tok") -> dict[str, str]:
    """`n` keys whose ciphertext never changes, to pad a synthetic history to realistic size."""
    return {f"{prefix}{i}": f"ENC[stable{i}]" for i in range(n)}


def test_reencrypt_guard_does_not_fire_on_an_ordinary_rotation():
    """Accept half: one key of many rotating for real must still date to its own commit.

    Only 1 of 10 shared keys changes here (10%), far below the 90% threshold, so the guard
    must not suppress it.
    """
    stable = _stable_keys(9)
    tools = _history_tools(
        [
            ("b", "2026-08-01", {"rotated": "ENC[new]", **stable}),
            ("a", "2026-01-01", {"rotated": "ENC[old]", **stable}),
        ]
    )
    assert ciphertext_rotation_dates(tools)["rotated"] == dt.date(2026, 8, 1)


def test_reencrypt_guard_suppresses_the_reencrypt_but_finds_the_real_earlier_rotation():
    """Reject half: a commit that changes every shared key's ciphertext is not 10 for 10 rotations.

    The guard must carry the walk past it to the real, earlier rotation one commit further
    back — exactly the shape 3e731bcec needed: it re-encrypted everything, but
    `arr_discord_webhook_url` had genuinely rotated days before.
    """
    stable = _stable_keys(9)
    tools = _history_tools(
        [
            # Whole-file re-encrypt: every one of the 10 shared keys gets a fresh
            # ciphertext, "rotated" included, even though only "rotated" really changed.
            (
                "d",
                "2026-09-09",
                {
                    "rotated": "ENC[rekeyed]",
                    **{k: f"{v}-rekeyed" for k, v in stable.items()},
                },
            ),
            ("c", "2026-06-01", {"rotated": "ENC[real-rotation]", **stable}),
            ("b", "2026-01-01", {"rotated": "ENC[original]", **stable}),
        ]
    )
    dates = ciphertext_rotation_dates(tools)
    assert dates["rotated"] == dt.date(2026, 6, 1)
    assert dates["tok0"] == dt.date(2026, 1, 1)


def test_a_key_introduced_at_a_reencrypt_commit_still_dates_to_it():
    """A key added at a whole-file re-encrypt commit is a real introduction, not noise.

    It must date to that commit — the `healthchecks_api_read_only_key` shape in 3e731bcec,
    which added a key on the same commit that re-encrypted everything else.
    """
    stable = _stable_keys(10)
    tools = _history_tools(
        [
            (
                "c",
                "2026-09-09",
                {
                    "new_tok": "ENC[created]",
                    **{k: f"{v}-rekeyed" for k, v in stable.items()},
                },
            ),
            ("b", "2026-05-01", stable),
        ]
    )
    dates = ciphertext_rotation_dates(tools)
    assert dates["new_tok"] == dt.date(2026, 9, 9)
    assert dates["tok0"] == dt.date(2026, 5, 1)


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
