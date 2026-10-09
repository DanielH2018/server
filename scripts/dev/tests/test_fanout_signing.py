"""The signing-key gate: which host GitHub verifies, and which it does not.

Run: uv run pytest scripts/dev/tests/test_fanout_signing.py
"""

import os
import subprocess

import pytest

from fanout_lib.signing import (
    fingerprint,
    normalize_key,
    signing_key_read_command,
    unverified_reason,
)

# daniel-server's real signing key. Used here as a syntactically real key the gate accepts
# when the registered set holds it — nothing in the gate compares against a constant, so
# rotating it breaks no test.
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
    """Measured with `ssh-keygen -l -f` against this key."""
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


def _run_key_read(tmp_path, signingkey: str) -> str:
    """Run the real key read in bash with `user.signingkey` set through the environment.

    The value goes in through GIT_CONFIG_* rather than `git config`, and every inherited
    GIT_* variable is dropped: a hook-run git honours GIT_DIR over `-C`, and a fixture that
    wrote config that way once rewrote the shared .git/config.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.signingkey",
        "GIT_CONFIG_VALUE_0": signingkey,
    }
    proc = subprocess.run(
        ["bash", "-c", signing_key_read_command(str(tmp_path))],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0
    return proc.stdout


def test_a_private_key_path_reads_the_public_key_beside_it(tmp_path):
    """The `claude` agent user's shape (#4098): the read printed the private key's 13 lines."""
    private = tmp_path / "git_signing_ed25519"
    private.write_text(PRIVATE_KEY_TEXT)
    (tmp_path / "git_signing_ed25519.pub").write_text(f"{SERVER_KEY} claude@daniel-box\n")
    out = _run_key_read(tmp_path, str(private))
    assert normalize_key(out) == SERVER_KEY
    assert PRIVATE_BODY not in out


def test_a_private_key_path_with_no_public_key_beside_it_prints_nothing(tmp_path):
    private = tmp_path / "git_signing_ed25519"
    private.write_text(PRIVATE_KEY_TEXT)
    assert _run_key_read(tmp_path, str(private)) == ""


def test_a_public_key_path_and_a_literal_key_still_read(tmp_path):
    public = tmp_path / "key.pub"
    public.write_text(f"{SERVER_KEY} ubuntu@daniel-server\n")
    assert normalize_key(_run_key_read(tmp_path, "~/key.pub")) == SERVER_KEY
    assert normalize_key(_run_key_read(tmp_path, SERVER_KEY)) == SERVER_KEY


@pytest.mark.parametrize(
    "key",
    [
        "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTY",
        "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t",
    ],
)
def test_normalize_key_accepts_every_type_github_takes_as_a_signing_key(key):
    """A rotation to ecdsa or a hardware key must not empty the registered set."""
    assert normalize_key(f"{key} ubuntu@somewhere\n") == key


@pytest.mark.parametrize("key", [SERVER_KEY, OTHER_KEY])
def test_every_key_in_this_file_is_one_normalize_key_accepts(key):
    """Non-vacuity: a regex that stopped matching would otherwise pass the flagged cases."""
    assert normalize_key(key) == key
