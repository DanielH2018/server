"""Reading a k8s role's own declarations out of its defaults, without a YAML parser.

The deployer runs under `uv run --no-project` and cannot import yaml, so `declared_denylist`
and `declares_snapshot_claims` are regexes over source text. Both are biased toward denied:
an absent, indented, duplicated or unparseable declaration counts as denied, and the
snapshot-claims regex is pinned against `yaml.safe_load` for every live role because the
claim cap reads it. `rollback_volume_revert_note` is the one line of the rollback alert that
says which services actually reverted.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_k8s_declarations.py

import pathlib

import yaml

from deploy_k8s import (
    SHARED_K8S_ROLES,
    declared_denylist,
    declares_snapshot_claims,
    k8s_role_paths,
    rollback_volume_revert_note,
)


def test_declared_denylist_collects_roles_declaring_false():
    sources = {
        "sonarr": 'k8s_autodeploy: false\nk8s_autodeploy_reason: "x"\n',
        "homepage": 'k8s_autodeploy: true\nk8s_autodeploy_reason: "y"\n',
    }
    assert declared_denylist(sources) == frozenset({"sonarr"})


def test_declared_denylist_ignores_the_shared_roles():
    sources = {name: None for name in SHARED_K8S_ROLES}
    sources["sonarr"] = "k8s_autodeploy: false\n"
    assert declared_denylist(sources) == frozenset({"sonarr"})


def test_an_unparseable_declaration_counts_as_denied():
    """Fail closed: a role we cannot read must not silently match the config's view."""
    sources = {"weird": "k8s_autodeploy: maybe\n", "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"weird"})


def test_a_missing_declaration_counts_as_denied():
    sources = {"silent": "some_other_var: 1\n", "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"silent"})


def test_a_missing_defaults_file_counts_as_denied():
    sources = {"gone": None, "ok": "k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"gone"})


def test_a_trailing_comment_does_not_break_parsing():
    sources = {"r": "k8s_autodeploy: false  # noqa var-naming[no-role-prefix]\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_yaml_no_and_off_read_as_false():
    """PyYAML resolves these to False, so the filter denies them; match that."""
    assert declared_denylist({"a": "k8s_autodeploy: no\n"}) == frozenset({"a"})
    assert declared_denylist({"b": "k8s_autodeploy: off\n"}) == frozenset({"b"})


def test_an_indented_key_is_not_a_declaration():
    """Only a top-level key declares. An indented one belongs to some other block."""
    sources = {"r": "something:\n  k8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset(
        {"r"}
    )  # denied: no top-level declaration


def test_a_duplicate_key_with_the_denial_first_reads_as_denied():
    sources = {"r": "k8s_autodeploy: false\nk8s_autodeploy: true\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_a_duplicate_key_with_the_denial_last_reads_as_denied():
    # YAML itself is last-key-wins, so a real parser would call this permitted. This reader
    # requires unanimity instead, which is strictly more conservative — see declared_denylist's
    # docstring for why that's the safe direction to diverge in.
    sources = {"r": "k8s_autodeploy: true\nk8s_autodeploy: false\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_no_space_after_the_colon_is_not_a_declaration():
    sources = {"r": "k8s_autodeploy:true\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_a_decoy_declaration_inside_a_quoted_scalar_reads_as_denied():
    # A multi-line quoted YAML scalar can contain a line that starts at column 0 and looks
    # exactly like a top-level `k8s_autodeploy: true` declaration, even though it's just text
    # inside some other key's value. The regex can't tell the difference — it isn't a YAML
    # parser — so it must not let that decoy line outvote the real, later `false`.
    sources = {
        "r": (
            'k8s_autodeploy_reason: "line one\n'
            "k8s_autodeploy: true\n"
            'still inside the quote"\n'
            "k8s_autodeploy: false\n"
        )
    }
    assert declared_denylist(sources) == frozenset({"r"})


def test_crlf_line_endings_are_not_recognized_and_so_deny():
    sources = {"r": "k8s_autodeploy: true\r\n"}
    assert declared_denylist(sources) == frozenset({"r"})


def test_declares_snapshot_claims_true_for_a_single_claim():
    assert declares_snapshot_claims(
        "k8s_autodeploy_snapshot_pvcs: [bazarr-config]  # noqa\n"
    )


def test_declares_snapshot_claims_true_for_a_two_claim_role():
    assert declares_snapshot_claims(
        "k8s_autodeploy_snapshot_pvcs: [tdarr-configs, tdarr-server]  # noqa\n"
    )


def test_declares_snapshot_claims_false_for_an_empty_list():
    assert not declares_snapshot_claims("k8s_autodeploy_snapshot_pvcs: []\n")


def test_declares_snapshot_claims_false_for_an_absent_key():
    assert not declares_snapshot_claims("some_other_var: 1\n")


def test_declares_snapshot_claims_false_for_none():
    assert not declares_snapshot_claims(None)


def test_declares_snapshot_claims_false_for_empty_string():
    assert not declares_snapshot_claims("")


def test_declares_snapshot_claims_ignores_an_indented_key():
    assert not declares_snapshot_claims(
        "something:\n  k8s_autodeploy_snapshot_pvcs: [x]\n"
    )


# declares_snapshot_claims() is a regex over source text. Since 2026-08-22 it is LOAD-BEARING,
# not just cosmetic: split_k8s_auto_deploy() uses it to decide which promotions count against
# gitops_deploy_k8s_autodeploy_max_claim_services_per_tick. That inverts the safe direction —
# for alert wording a False on unparseable input under-claims (harmless), but for gating it
# reads as claim-free and lets the service batch, which is the exact overrun the cap prevents.
# This test is what holds that closed, so treat a failure here as a deploy-safety failure.
#
# roles/k8s/manifests decides the REAL revert from `yaml.safe_load`'d defaults —
# a different reader of the same file. All 13 roles that declare
# `k8s_autodeploy_snapshot_pvcs` today write it as a single-line list literal, so nothing has
# ever exercised the gap: reformat one to block style and the regex returns False (no revert
# applies, says the alert) while the volume still reverts for real. This walks every role's
# actual defaults/main.yml and pins that the two readers agree on all of them, so a future
# reformat fails this test instead of surfacing as an incident alert that names the wrong thing.
_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"


def _yaml_declares_claims(text: str) -> bool:
    data = yaml.safe_load(text) or {}
    return bool(data.get("k8s_autodeploy_snapshot_pvcs"))


def test_declares_snapshot_claims_agrees_with_yaml_for_every_k8s_role():
    mismatches = []
    for defaults_path in sorted(_K8S_ROLES_DIR.glob("*/defaults/main.yml")):
        text = defaults_path.read_text()
        regex_verdict = declares_snapshot_claims(text)
        yaml_verdict = _yaml_declares_claims(text)
        if regex_verdict != yaml_verdict:
            mismatches.append(
                f"{defaults_path.relative_to(_K8S_ROLES_DIR.parent)}: "
                f"regex={regex_verdict} yaml={yaml_verdict}"
            )
    assert not mismatches, (
        "declares_snapshot_claims()'s regex disagrees with what roles/k8s/manifests actually "
        "reads via yaml.safe_load for:\n" + "\n".join(mismatches)
    )


def test_rollback_volume_revert_note_reports_the_redeploy_failure_when_it_failed():
    """The redeploy raising means the revert task inside roles/k8s/manifests may never have
    run — the note must say so, not claim a revert was attempted."""
    note = rollback_volume_revert_note({"sonarr"}, frozenset(), "boom")
    assert "rollback redeploy itself failed" in note
    assert "boom" in note
    assert "Volume revert" not in note


def test_rollback_volume_revert_note_says_no_claims_when_none_declare():
    note = rollback_volume_revert_note({"speedtest"}, frozenset(), None)
    assert "No service" in note
    assert "no volume revert applies" in note


def test_rollback_volume_revert_note_names_only_the_services_that_revert():
    note = rollback_volume_revert_note(
        {"sonarr", "speedtest"}, frozenset({"sonarr"}), None
    )
    assert "`sonarr`" in note
    assert "speedtest" in note  # named as unaffected, not silently dropped
    assert "declares no `k8s_autodeploy_snapshot_pvcs` and is unaffected" in note


def test_rollback_volume_revert_note_omits_the_unaffected_aside_when_all_revert():
    note = rollback_volume_revert_note({"sonarr"}, frozenset({"sonarr"}), None)
    assert "unaffected" not in note


def test_k8s_role_paths_finds_a_normal_role():
    listing = "ansible/roles/k8s/sonarr/defaults/main.yml\nansible/roles/k8s/sonarr/tasks/main.yml\n"
    assert k8s_role_paths(listing) == {
        "sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml"
    }


def test_k8s_role_paths_a_role_with_no_defaults_maps_to_none():
    listing = "ansible/roles/k8s/homepage/tasks/main.yml\n"
    assert k8s_role_paths(listing) == {"homepage": None}


def test_k8s_role_paths_a_defaults_dir_holding_something_other_than_main_yml():
    listing = "ansible/roles/k8s/sonarr/defaults/other.yml\n"
    assert k8s_role_paths(listing) == {"sonarr": None}


def test_k8s_role_paths_ignores_a_stray_file_directly_under_roles_k8s():
    listing = "ansible/roles/k8s/README.md\n"
    assert k8s_role_paths(listing) == {}


def test_k8s_role_paths_empty_listing():
    assert k8s_role_paths("") == {}


def test_k8s_role_paths_order_does_not_matter():
    before_after = (
        "ansible/roles/k8s/sonarr/tasks/main.yml\n"
        "ansible/roles/k8s/sonarr/defaults/main.yml\n"
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2\n"
    )
    after_before = (
        "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2\n"
        "ansible/roles/k8s/sonarr/defaults/main.yml\n"
        "ansible/roles/k8s/sonarr/tasks/main.yml\n"
    )
    expected = {"sonarr": "ansible/roles/k8s/sonarr/defaults/main.yml"}
    assert k8s_role_paths(before_after) == expected
    assert k8s_role_paths(after_before) == expected
