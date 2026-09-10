"""The signing-key gate: which host GitHub verifies, and which it does not — issue #1615.

Run: uv run pytest scripts/dev/tests/test_fanout_signing.py
"""

import pytest

from fanout_lib.signing import (
    fingerprint,
    normalize_key,
    signing_key_read_command,
    unverified_reason,
)

# daniel-server's real signing key, the one whose registration closed #1615. Used here as a
# syntactically real key the gate accepts when the registered set holds it — nothing in the
# gate compares against a constant, so rotating it breaks no test.
SERVER_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINLm/Wigk9BPb5s+SAPzJFnOzOhyKEWcBf3WbeCqfndo"
)
SERVER_FINGERPRINT = "SHA256:cDMo/8UD9tIrQBq05HxlDsmbn6Cy2xmkNOruE64pemE"
OTHER_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)
# What a host prints when `user.signingkey` names a PRIVATE key file: the read the gate must
# refuse, and must not echo. Assembled from pieces so gitleaks does not read this test data
# as a committed key.
PRIVATE_BODY = "b3BlbnNzaC1r" + "ZXktdjEA"
PRIVATE_KEY_TEXT = "-----BEGIN OPENSSH " + f"PRIVATE KEY-----\n{PRIVATE_BODY}\n"


def test_normalize_key_drops_the_comment_so_the_two_sides_compare_equal():
    assert normalize_key(f"{SERVER_KEY} ubuntu@daniel-server-ansible\n") == SERVER_KEY


def test_normalize_key_is_flagged_on_anything_that_is_not_a_public_key():
    """None on both sides must never compare equal, which is how a gate passes everything."""
    for text in (
        "",
        None,
        "fatal: not a git repository\n",
        PRIVATE_KEY_TEXT,
        "ssh-ed25519\n",
    ):
        assert normalize_key(text) is None


def test_fingerprint_matches_what_ssh_keygen_prints():
    """Measured with `ssh-keygen -l -f` against this key on 2026-09-10."""
    assert fingerprint(SERVER_KEY) == SERVER_FINGERPRINT


def test_fingerprint_is_unreadable_rather_than_raising_on_a_malformed_blob():
    assert fingerprint("ssh-ed25519 not-base64!") == "SHA256:<unreadable>"


def test_a_registered_key_is_clean():
    registered = frozenset({SERVER_KEY})
    assert (
        unverified_reason("daniel-server", f"{SERVER_KEY} comment\n", registered)
        is None
    )


def test_an_unregistered_key_is_flagged_and_names_its_fingerprint():
    reason = unverified_reason("daniel-server", SERVER_KEY, frozenset({OTHER_KEY}))
    assert reason is not None
    assert "daniel-server" in reason and SERVER_FINGERPRINT in reason
    assert "unknown_key" in reason


def test_an_unreadable_key_is_flagged_and_echoes_no_key_material():
    secret = PRIVATE_KEY_TEXT
    reason = unverified_reason("daniel-server", secret, frozenset({SERVER_KEY}))
    assert reason is not None and PRIVATE_BODY not in reason


def test_an_empty_registered_set_flags_every_host():
    """A gh reply with no keys must not read as "nothing to check"."""
    assert unverified_reason("daniel-box", SERVER_KEY, frozenset()) is not None


def test_the_key_read_command_is_one_read_only_line_naming_the_repo():
    command = signing_key_read_command("/home/ubuntu/server")
    assert "\n" not in command
    assert "git -C /home/ubuntu/server config --get user.signingkey" in command
    for verb in ("rm", "systemctl", ">", "sudo", "kill"):
        assert verb not in command


@pytest.mark.parametrize("key", [SERVER_KEY, OTHER_KEY])
def test_every_key_in_this_file_is_one_normalize_key_accepts(key):
    """Non-vacuity: a regex that stopped matching would otherwise pass the flagged cases."""
    assert normalize_key(key) == key
