"""`cluster_vars.py check`'s assertions, each an `_is_clean` / `_is_flagged` pair (slice D3).

The pairs run on synthetic text: no sops, no key and no render. Fixture ciphertext is built by
concatenation, so no line of this file opens with a SOPS MAC line and the protected-edits
hook keeps it editable. `test_cluster_vars_write.py` covers the assertions that need a real
encrypt: the MAC proxy's written half, and a manifest encrypted by the wrong rule.

Run: uv run pytest scripts/secrets_mgmt/tests/test_cluster_vars_check.py
"""

from dataclasses import replace

import pytest

from _cluster_vars_fixture import EXPECTED
from secrets_mgmt.cluster_vars import fleet_expected, main
from secrets_mgmt.cluster_vars_check import (
    SourceLine,
    Tree,
    check_tree,
    ciphertext_problems,
    coverage_problems,
    digest,
    file_recipients,
    hash_problems,
    mac_proxy_problems,
    manifest_problems,
    manifest_set_problems,
    names_problems,
    parse_source,
    recipient_problems,
    rule_recipients,
    source_mapping_problems,
    source_text,
)

ENC = "ENC[" + "AES256_GCM,data:Zz==,iv:Yy=,tag:Xx==,type:str]"
ENC_INT = "ENC[" + "AES256_GCM,data:Zz==,iv:Yy=,tag:Xx==,type:int]"


def _dotenv(body: dict[str, str], mac: str = ENC, stamp: str = "T1") -> str:
    lines = [f"{k}={v}" for k, v in body.items()]
    lines += ["sops_age__list_0__map_recipient=age1one", f"sops_lastmodified={stamp}"]
    return "\n".join(lines + ["sops_mac=" + mac, "sops_version=3.13.3"]) + "\n"


def _manifest(
    key: str = "blob",
    value: str = ENC,
    annotated: bool = True,
    name: str = "demo-blob",
) -> str:
    annotation = (
        "  annotations:\n    kustomize.toolkit.fluxcd.io/substitute: disabled\n"
        if annotated
        else ""
    )
    return (
        f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {name}\n"
        f"  namespace: homelab\n{annotation}type: Opaque\nstringData:\n"
        f"  {key}: {value}\n"
        "sops:\n  age:\n    - recipient: age1one\n"
        '  lastmodified: "T1"\n  mac: ' + ENC + "\n"
    )


RENDERED = EXPECTED.manifests[0].text


def test_ciphertext_values_are_clean():
    assert ciphertext_problems("f", _dotenv({"a": ENC}), dotenv=True) == []
    assert ciphertext_problems("m", _manifest(), dotenv=False) == []


def test_a_plaintext_dotenv_value_is_flagged_by_name_alone():
    found = ciphertext_problems("f", _dotenv({"a": ENC, "b": "hunter2"}), dotenv=True)
    assert found == ["f: b is not SOPS ciphertext"]


def test_a_plaintext_manifest_value_is_flagged():
    found = ciphertext_problems("m", _manifest(value="hunter2"), dotenv=False)
    assert found == ["m: stringData.blob is not SOPS ciphertext"]


def test_a_dropped_line_with_both_mac_lines_moved_is_clean():
    base = _dotenv({"a": ENC, "b": ENC})
    now = _dotenv({"a": ENC}, mac=ENC_INT, stamp="T2")
    assert mac_proxy_problems("f", now, base, dotenv=True) == []


def test_a_dropped_line_with_the_mac_lines_unchanged_is_flagged():
    base = _dotenv({"a": ENC, "b": ENC})
    found = mac_proxy_problems("f", _dotenv({"a": ENC}), base, dotenv=True)
    assert found and "MAC and lastmodified did not" in found[0]


def test_a_file_absent_from_its_base_is_exempt_from_the_mac_proxy():
    assert mac_proxy_problems("f", _dotenv({"a": ENC}), None, dotenv=True) == []


def test_a_manifest_edited_by_hand_is_flagged():
    base = _manifest()
    found = mac_proxy_problems("m", _manifest(key="renamed"), base, dotenv=False)
    assert found


CONFIG = (
    "creation_rules:\n"
    "  - path_regex: vars/cluster-vars\\.sops\\.env$\n    age: 'c1,h1,h2'\n"
    "  - path_regex: vars/cluster-manifests/[^/]+\\.sops\\.yaml$\n    age: 'c1,h1,h2'\n"
    "  - path_regex: .*(vars|secrets)\\/.*\\.ya?ml$\n    age: 'h1,h2'\n"
)


def test_the_first_matching_rule_decides_a_manifests_recipients():
    """The generic rule matches a manifest too; the cluster rule above it must win."""
    assert rule_recipients(CONFIG, "vars/cluster-manifests/a.b.sops.yaml") == {
        "c1",
        "h1",
        "h2",
    }
    assert rule_recipients(CONFIG, "vars/secrets.yml") == {"h1", "h2"}


def test_recipients_equal_to_the_rule_are_clean():
    assert recipient_problems("f", {"c1", "h1", "h2"}, {"c1", "h1", "h2"}) == []


def test_a_cluster_file_missing_one_host_from_its_rule_is_flagged():
    have = file_recipients(_dotenv({"a": ENC}), dotenv=True)
    assert have == {"age1one"}
    assert recipient_problems("f", have, {"age1one", "age1two"})


def test_a_source_digest_matching_the_ciphertext_is_clean():
    source = {"a": SourceLine(digest(ENC))}
    assert hash_problems(source, {"a": ENC}) == []


def test_a_secret_changed_since_the_cluster_file_was_written_is_flagged():
    """The decisive assertion: a cluster key edited in secrets.yml without `write`."""
    source = {"a": SourceLine(digest(ENC))}
    assert hash_problems(source, {"a": ENC_INT}) == [
        "a changed in secrets.yml since the cluster files were written"
    ]
    assert hash_problems(source, {}) == [
        ".source names a, which secrets.yml no longer holds"
    ]


def test_source_round_trips_through_its_text():
    lines = {"b": SourceLine("d2", "x.sops.yaml"), "a": SourceLine("d1")}
    assert parse_source(source_text(lines)) == lines


def test_source_covering_every_held_key_and_manifest_is_clean():
    source = {"a": SourceLine("d"), "k": SourceLine("d", "m.sops.yaml")}
    assert coverage_problems(source, {"a"}, ["m.sops.yaml"]) == []


def test_source_missing_a_held_key_or_manifest_is_flagged():
    assert coverage_problems({}, {"a"}, ["m.sops.yaml"]) == [
        ".source does not cover a, which the cluster file holds",
        ".source names no key of the manifest m.sops.yaml",
    ]


def test_source_naming_a_key_or_manifest_nothing_holds_is_flagged():
    source = {"a": SourceLine("d"), "k": SourceLine("d", "gone.sops.yaml")}
    assert len(coverage_problems(source, set(), [])) == 2


def test_dotenv_names_equal_to_the_placeholder_set_are_clean():
    assert names_problems({"a", "b"}, frozenset({"a", "b"})) == []


def test_a_placeholder_missing_from_the_dotenv_is_flagged():
    assert names_problems({"a"}, frozenset({"a", "b"})) == [
        "the cluster render references b, and the cluster file does not hold it"
    ]
    assert names_problems({"a", "c"}, frozenset({"a"}))


def test_one_manifest_per_entry_is_clean():
    names = [entry.name for entry in EXPECTED.manifests]
    assert manifest_set_problems(EXPECTED.manifests, names) == []


def test_a_missing_or_unknown_manifest_is_flagged():
    found = manifest_set_problems(
        EXPECTED.manifests, ["demo.blob-secret.sops.yaml", "x"]
    )
    assert found == [
        "no manifest acct.number-secret.sops.yaml for acct's entry number-secret.yaml",
        "the manifest x matches no render_sops_manifests entry",
    ]


def test_source_filing_each_manifest_key_under_its_manifest_is_clean():
    entry = EXPECTED.manifests[0]
    source = {"blob_key": SourceLine("d", entry.name)}
    assert source_mapping_problems(source, [entry]) == []


def test_source_filing_a_key_under_another_manifest_is_flagged():
    entry = EXPECTED.manifests[0]
    assert source_mapping_problems({"blob_key": SourceLine("d", "x")}, [entry])


def test_an_annotated_manifest_matching_its_template_is_clean():
    assert manifest_problems("m", _manifest(), RENDERED) == []


def test_a_manifest_without_the_substitute_annotation_is_flagged():
    found = manifest_problems("m", _manifest(annotated=False), RENDERED)
    assert found == [
        "m lacks the annotation kustomize.toolkit.fluxcd.io/substitute: disabled"
    ]


def test_a_manifest_with_a_renamed_data_key_is_flagged():
    found = manifest_problems("m", _manifest(key="renamed"), RENDERED)
    assert found == ["m no longer matches the structure its template renders"]


def test_a_manifest_value_encrypted_from_an_int_is_flagged():
    found = manifest_problems("m", _manifest(value=ENC_INT), RENDERED)
    assert found == ["m: stringData.blob was encrypted from a int, not a string"]


def _tree(tmp_path) -> Tree:
    tree = Tree(tmp_path)
    tree.secrets.parent.mkdir(parents=True)
    tree.secrets.write_text("a: " + ENC + "\n")
    tree.sops_config.write_text(CONFIG)
    return tree


def test_an_empty_tree_is_clean_without_a_render(tmp_path):
    """D3.2a is inert: the pre-commit hook passes while no cluster file exists."""
    assert check_tree(_tree(tmp_path), None, lambda _p: None) == []


def test_an_empty_tree_is_flagged_with_a_render(tmp_path):
    found = check_tree(
        _tree(tmp_path), replace(EXPECTED, manifests=()), lambda _p: None
    )
    assert found == ["ansible/vars/cluster-vars.sops.env does not exist"]


def test_ciphertext_only_is_clean_on_ciphertext(tmp_path):
    f = tmp_path / "copy.env"
    f.write_text(_dotenv({"a": ENC}))
    assert main(["check", "--ciphertext-only", str(f)]) == 0


def test_ciphertext_only_is_flagged_on_plaintext(tmp_path, capsys):
    f = tmp_path / "copy.sops.yaml"
    f.write_text(_manifest(value="hunter2"))
    assert main(["check", "--ciphertext-only", str(f)]) == 1
    err = capsys.readouterr().err
    assert "stringData.blob is not SOPS ciphertext" in err and "hunter2" not in err


def test_ciphertext_only_refuses_a_directory(tmp_path):
    with pytest.raises(SystemExit) as exited:
        main(["check", "--ciphertext-only", str(tmp_path)])
    assert exited.value.code == 64


def test_the_real_render_names_its_known_members():
    """The real provider, end to end. Non-vacuity for every set `check` compares against.

    `domain` is published (C3) and must not be a cluster key; `email` stays one. peanut is
    D3's first flux-owned Secret. The four `render_sops_manifests` entries are R2's.
    """
    expected = fleet_expected()
    assert {"email", "peanut_password", "peanut_username", "nut_ha_password"} <= (
        expected.keys
    )
    assert "domain" not in expected.keys
    assert "email" in expected.standalone
    assert {e.tag for e in expected.manifests} == {
        "authelia",
        "home-assistant",
        "pi-peer-backup",
        "qbittorrent",
    }
    assert all(entry.keys for entry in expected.manifests)
    assert not expected.keys & {k for e in expected.manifests for k in e.keys}
