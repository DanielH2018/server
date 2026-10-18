"""D3.2d's per-key refusals and D5's successors, each run through `cluster_vars.py write` on a fixture tree.

Every test runs the real `sops` against a throwaway age key from `_cluster_vars_fixture.py`.
The digests are literals in the base64 alphabet, never copied from the real `secrets.yml`.

`authelia_k8s_manage_claude_user: false` reaches `write` only as a render that does not
reference the claude-ui digest, so the test of that case drops the key from `Expected`.
`ansible/tests/services/test_authelia_digest_assert.py` holds the real render to that.

Run: uv run pytest scripts/secrets_mgmt/tests/test_cluster_vars_write_refusals.py
"""

import re
from dataclasses import replace

import pytest

from _cluster_vars_fixture import EXPECTED, VALUES, make_sops_tree
from secrets_mgmt.cluster_vars import write
from secrets_mgmt.cluster_vars_check import dotenv_pairs

_PREFIX = "$argon2id$v=19$m=65536,t=3,p=4$"
ADMIN = (
    _PREFIX + "QWRtaW5TYWx0Rml4dHVyZQ" + "$" + "a" * 20 + "AdminKeyFixture" + "b" * 8
)
CLAUDE = (
    _PREFIX + "Q2xhdWRlU2FsdEZpeHR1cg" + "$" + "c" * 20 + "ClaudeKeyFixtur" + "d" * 8
)
AGENT = (
    _PREFIX + "QWdlbnRTYWx0Rml4dHVyZQ" + "$" + "e" * 20 + "AgentKeyFixture" + "f" * 8
)

GOOD = {
    "authelia_password_argon2": ADMIN,
    "authelia_claude_password_argon2": CLAUDE,
    "authelia_agent_password_argon2": AGENT,
    "mqtt_password_hash": "$7$abc",
    "fixture_tile_push_token": "a" * 32,
}
PLACED = replace(EXPECTED, keys=EXPECTED.keys | set(GOOD))
NOT_A_DIGEST = "not an argon2id digest"


@pytest.fixture
def written(tmp_path):
    """A fixture tree after a clean first write of the well-formed set."""
    sops_tree = make_sops_tree(tmp_path, {**VALUES, **GOOD})
    assert write(sops_tree.tree, PLACED, run=sops_tree.run) == 0
    return sops_tree


def test_a_distinct_well_formed_set_and_a_bare_mosquitto_hash_are_clean(tmp_path):
    """The clean half of each refusal below: the same tree with only the bad value swapped."""
    sops_tree = make_sops_tree(tmp_path, {**VALUES, **GOOD})
    assert write(sops_tree.tree, PLACED, run=sops_tree.run) == 0
    held = dotenv_pairs(sops_tree.decrypt(sops_tree.tree.cluster_file))
    assert {k: held[k] for k in GOOD} == GOOD


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("authelia_password_argon2", ADMIN.replace("t=3", "t=2"), NOT_A_DIGEST),
        ("authelia_password_argon2", "Digest: " + ADMIN, NOT_A_DIGEST),
        ("authelia_claude_password_argon2", CLAUDE + " ", NOT_A_DIGEST),
        ("authelia_agent_password_argon2", "fixture-plain-password-q4", NOT_A_DIGEST),
        ("authelia_agent_password_argon2", ADMIN, "the same digest as"),
        ("mqtt_password_hash", "user:$7$abc", "holds a `:`"),
        # The shape a base64 mint produces: 44 characters, padded with `=`.
        ("fixture_tile_push_token", "QUJD" * 10 + "QQ+=", "not 32 letters and digits"),
        (
            "fixture_tile_push_token",
            "0123456789abcdef-0123456789abcd",
            "not 32 letters",
        ),
    ],
    ids=[
        "t=2",
        "Digest-prefix",
        "trailing-space",
        "plaintext",
        "equal",
        "mqtt-colon",
        "push-base64",
        "push-hyphen",
    ],
)
def test_a_refused_value_changes_no_file_and_names_only_the_key(
    written, capfd, key, value, reason
):
    written.set_secrets({**VALUES, **GOOD, key: value})
    before = written.snapshot()
    capfd.readouterr()
    assert write(written.tree, PLACED, run=written.run) == 1
    err = capfd.readouterr().err
    assert written.snapshot() == before
    refused = [line.strip() for line in err.splitlines() if reason in line]
    assert any(line.startswith(f"{key}: ") for line in refused), err
    # The whole value, and each salt, key or plaintext run in it.
    for text in [value, *(p for p in re.split(r"[$:]", value) if len(p) > 8)]:
        assert text not in err, f"the refusal printed part of {key}'s value"


def test_two_equal_digests_name_both_keys(tmp_path, capfd):
    sops_tree = make_sops_tree(
        tmp_path, {**VALUES, **GOOD, "authelia_claude_password_argon2": ADMIN}
    )
    assert write(sops_tree.tree, PLACED, run=sops_tree.run) == 1
    err = capfd.readouterr().err
    assert "authelia_password_argon2: holds the same digest as " in err
    assert "authelia_claude_password_argon2: holds the same digest as " in err
    assert not sops_tree.tree.cluster_file.exists()


@pytest.mark.parametrize(("placed", "code"), [(True, 1), (False, 0)])
def test_a_malformed_claude_digest_is_checked_only_when_placed(tmp_path, placed, code):
    """With `authelia_k8s_manage_claude_user` false, the render places no claude-ui digest.

    `ansible/tests/services/test_authelia_digest_assert.py` holds the render to that.
    """
    sops_tree = make_sops_tree(
        tmp_path,
        {**VALUES, **GOOD, "authelia_claude_password_argon2": "Digest: plain-x7"},
    )
    keys = PLACED.keys - (set() if placed else {"authelia_claude_password_argon2"})
    assert write(sops_tree.tree, replace(PLACED, keys=keys), run=sops_tree.run) == code
