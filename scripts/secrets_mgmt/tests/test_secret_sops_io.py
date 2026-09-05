"""Tests for the push-token shape check in scripts/secrets_mgmt/sops_io.py.

Synthetic values throughout: CI has no age key, so a test may never touch the real
secrets.yml. `malformed_push_tokens` is pure for exactly that reason — `cmd_audit` hands it
what `sops_decrypt` returned, and nothing here decrypts anything.

The accept/reject pair is the point. A shape check that flags everything and one that flags
nothing look identical from the passing side, so each rule below has an input it must clear
and an input it must catch.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_sops_io.py
"""

import secrets as pysecrets

from secrets_mgmt.sops_io import malformed_push_tokens


def test_push_token_shape_is_clean():
    values = {
        "ruleset_drift_push_token": "a" * 32,
        # Built rather than written out: a 32-char hex literal, even a fake one, trips
        # gitleaks' generic-api-key entropy rule in the pre-commit hook.
        "monitor_bridge_pvc_push_token": "ab12" * 8,
        "mixed_case_push_token": "aB3" + "x" * 29,
        "cloudflare_api_token": "short-and-not-a-push-token",
    }
    assert malformed_push_tokens(values) == []


def test_push_token_shape_is_flagged():
    values = {
        "short_push_token": "a" * 30,  # the live PR #675 defect
        "long_push_token": "a" * 33,
        "punctuated_push_token": "a" * 31 + "-",  # right length, wrong character class
        "nonstring_push_token": 12345,
        "good_push_token": "a" * 32,
    }
    flagged = dict(malformed_push_tokens(values))
    assert set(flagged) == {
        "short_push_token",
        "long_push_token",
        "punctuated_push_token",
        "nonstring_push_token",
    }
    assert "30 chars" in flagged["short_push_token"]
    assert "non-alphanumeric" in flagged["punctuated_push_token"]
    # Reasons reach a Kuma push message and stdout, so they must never carry the value.
    assert not any("aaaa" in reason for reason in flagged.values())


def test_rotate_mints_a_token_the_shape_check_accepts():
    """The auto-rotation path and the guard must agree, or every rotation flips the monitor.

    `cmd_rotate` mints `token_hex(16)`; this is the assertion that the two stay in step.
    """
    minted = {"x_push_token": pysecrets.token_hex(16)}
    assert malformed_push_tokens(minted) == []
