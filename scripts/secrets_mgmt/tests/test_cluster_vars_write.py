"""`cluster_vars.py write`, run against a throwaway age key in `tmp_path` (target slice D3).

Every test here runs the real `sops` against a fixture tree from `_cluster_vars_fixture.py`,
whose keys `age-keygen` makes per test. No test reads the real `secrets.yml`. The value
refusals are checked on `value_refusals` directly, and once end to end through `write`.

Run: uv run pytest scripts/secrets_mgmt/tests/test_cluster_vars_write.py
"""

import subprocess
import time
from dataclasses import replace
from functools import partial

import pytest

from _cluster_vars_fixture import EXPECTED, VALUES, make_sops_tree, plaintexts
from lib.git_testing import commit, init_repo
from secrets_mgmt.cluster_vars import main, value_refusals, write
from secrets_mgmt.cluster_vars_check import (
    check_tree,
    dotenv_pairs,
    file_recipients,
    mac_proxy_problems,
)


def _no_base(_path):
    return None


@pytest.fixture
def sops_tree(tmp_path):
    return make_sops_tree(tmp_path)


def test_a_first_write_is_clean_under_check_and_decrypts_to_the_values(sops_tree):
    tree = sops_tree.tree
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    assert check_tree(tree, EXPECTED, _no_base) == []
    held = dotenv_pairs(sops_tree.decrypt(tree.cluster_file))
    assert held == {k: VALUES[k] for k in EXPECTED.keys}
    # The all-digit value went in as a string, so SOPS recorded `type:str`.
    number = tree.manifests / "acct.number-secret.sops.yaml"
    assert 'account: "7364519"' in sops_tree.decrypt(number)


def test_a_no_change_write_leaves_every_file_byte_identical(sops_tree):
    assert write(sops_tree.tree, EXPECTED, run=sops_tree.run) == 0
    before = sops_tree.snapshot()
    assert write(sops_tree.tree, EXPECTED, run=sops_tree.run) == 0
    assert sops_tree.snapshot() == before


def test_a_changed_value_moves_only_its_own_ciphertext(sops_tree):
    tree = sops_tree.tree
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    before = dotenv_pairs(tree.cluster_file.read_text())
    sops_tree.set_secrets({**VALUES, "alpha_one": "fixture-alpha-rotated"})
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    after = dotenv_pairs(tree.cluster_file.read_text())
    assert after["alpha_one"] != before["alpha_one"]
    assert after["beta_two"] == before["beta_two"]
    assert check_tree(tree, EXPECTED, _no_base) == []


def test_write_from_outside_ansible_encrypts_to_the_cluster_rule(sops_tree, tmp_path):
    """sops resolves `.sops.yaml` from its working directory, which `--config` overrides.

    The working directory holds no `.sops.yaml` above it, so an encrypt without `--config`
    finds no creation rule and fails. The first write encrypts every file from stdin, the
    path that resolves a rule.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    run = partial(sops_tree.run, cwd=outside)
    assert write(sops_tree.tree, EXPECTED, run=run) == 0
    want = {sops_tree.cluster, sops_tree.host}
    for path in [sops_tree.tree.cluster_file, *sops_tree.tree.manifest_files()]:
        text = path.read_text()
        assert file_recipients(text, path.suffix == ".env") == want, path.name


def test_write_leaves_no_fixture_plaintext_in_any_file_or_its_output(sops_tree, capfd):
    tree = sops_tree.tree
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    rotated = {**VALUES, "beta_two": "fixture-beta-rotated-m4", "blob_key": "q7-blob\n"}
    sops_tree.set_secrets(rotated)
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    printed = "".join(capfd.readouterr())
    files = sops_tree.snapshot()
    for text in plaintexts() + plaintexts(rotated):
        assert text not in printed, "write printed a value"
        leaked = [name for name, data in files.items() if text.encode() in data]
        assert not leaked, f"a fixture value is in {leaked}"


def _failing_encrypt(run):
    def wrapped(cmd, **kwargs):
        if "encrypt" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return run(cmd, **kwargs)

    return wrapped


def test_a_failed_first_encrypt_adds_no_file(sops_tree):
    before = sops_tree.snapshot()
    assert write(sops_tree.tree, EXPECTED, run=_failing_encrypt(sops_tree.run)) == 1
    assert sops_tree.snapshot() == before


def test_a_failed_manifest_encrypt_changes_no_file(sops_tree):
    """A changed dotenv value is pending too, and its `sops set` must not run either."""
    assert write(sops_tree.tree, EXPECTED, run=sops_tree.run) == 0
    sops_tree.set_secrets({**VALUES, "alpha_one": "fixture-x1", "blob_key": "fresh\n"})
    before = sops_tree.snapshot()
    assert write(sops_tree.tree, EXPECTED, run=_failing_encrypt(sops_tree.run)) == 1
    assert sops_tree.snapshot() == before


def test_a_refused_value_changes_no_file_and_is_named_without_its_value(
    sops_tree, capfd
):
    assert write(sops_tree.tree, EXPECTED, run=sops_tree.run) == 0
    sops_tree.set_secrets({**VALUES, "beta_two": 'fixture "quoted" v8'})
    before = sops_tree.snapshot()
    assert write(sops_tree.tree, EXPECTED, run=sops_tree.run) == 1
    err = capfd.readouterr().err
    assert "beta_two" in err and "quoted" not in err
    assert sops_tree.snapshot() == before


def test_a_written_dropped_key_moves_both_mac_lines_and_a_hand_edit_moves_neither(
    sops_tree,
):
    """The MAC proxy's pair. `write` drops a key through `sops unset`, which rewrites the MAC
    and `lastmodified`; deleting the same line by hand leaves both as they were.
    """
    tree = sops_tree.tree
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    base = tree.cluster_file.read_text()
    hand = "".join(
        line for line in base.splitlines(True) if not line.startswith("beta_two=")
    )
    assert mac_proxy_problems("f", hand, base, dotenv=True)
    # `lastmodified` has a one-second resolution.
    time.sleep(1.1)
    dropped = replace(EXPECTED, keys=EXPECTED.keys - {"beta_two"})
    assert write(tree, dropped, run=sops_tree.run) == 0
    written = tree.cluster_file.read_text()
    assert "beta_two=" not in written
    assert mac_proxy_problems("f", written, base, dotenv=True) == []


def test_check_through_git_flags_a_hand_edit_and_names_write(sops_tree, capfd):
    """`check --no-render --base HEAD`, the pre-commit hook's command, end to end."""
    tree = sops_tree.tree
    init_repo(tree.root)
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    commit(tree.root, "cluster files")
    assert main(["check", "--no-render", "--base", "HEAD"], tree=tree) == 0
    text = tree.cluster_file.read_text()
    tree.cluster_file.write_text(
        "".join(l for l in text.splitlines(True) if not l.startswith("beta_two="))
    )
    capfd.readouterr()
    assert main(["check", "--no-render", "--base", "HEAD"], tree=tree) == 1
    err = capfd.readouterr().err
    assert "edited by hand, not written by sops" in err
    assert "cluster_vars.py write" in err


def test_a_manifest_encrypted_by_the_generic_rule_is_flagged(sops_tree):
    """The generic rule matches a manifest too. A check that ignored rule order would accept a
    manifest encrypted to the host key alone, which the cluster cannot decrypt.
    """
    tree = sops_tree.tree
    assert write(tree, EXPECTED, run=sops_tree.run) == 0
    manifest = tree.manifests / "acct.number-secret.sops.yaml"
    plaintext = sops_tree.decrypt(manifest)
    manifest.write_text(
        sops_tree.encrypt_to(plaintext, str(tree.ansible / "vars" / "x.yaml"), "yaml")
    )
    found = check_tree(tree, EXPECTED, _no_base)
    assert any("acct.number-secret.sops.yaml is encrypted to 1" in p for p in found)


@pytest.mark.parametrize(
    "value", ["a\nb", 'a"b', "a\\b", "a\x07b", "a{{b", "a\rb"], ids=repr
)
def test_a_forbidden_character_is_refused_by_key_name_alone(value):
    refused = value_refusals({**VALUES, "alpha_one": value}, EXPECTED)
    assert refused and all(line.startswith("alpha_one: ") for line in refused)
    assert all(value not in line for line in refused)


def test_an_empty_value_is_refused():
    """The pinned SOPS writes an empty dotenv value as a plain `key=` line, unencrypted."""
    refused = value_refusals({**VALUES, "alpha_one": ""}, EXPECTED)
    assert [line.split(":")[0] for line in refused] == ["alpha_one"]


def test_a_single_brace_passes():
    assert value_refusals({**VALUES, "alpha_one": "a{b}c"}, EXPECTED) == []


@pytest.mark.parametrize("value", ["0123", "yes", "1e3"])
def test_a_typed_standalone_value_is_refused(value):
    refused = value_refusals({**VALUES, "gamma_flag": value}, EXPECTED)
    assert [line.split(":")[0] for line in refused] == ["gamma_flag"]


@pytest.mark.parametrize("value", ["0123", "yes", "1e3"])
def test_the_same_value_inside_other_text_passes(value):
    assert value_refusals({**VALUES, "alpha_one": value}, EXPECTED) == []


def test_a_referenced_key_absent_from_secrets_yml_is_refused():
    values = {k: v for k, v in VALUES.items() if k != "blob_key"}
    assert value_refusals(values, EXPECTED) == [
        "blob_key: named by demo.blob-secret.sops.yaml and absent from secrets.yml"
    ]
