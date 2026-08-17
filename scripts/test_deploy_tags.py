#!/usr/bin/env python3
"""Tests for the deploy --tags guard.

Half of these run against the REAL inventory rather than a fixture. That is deliberate:
the failure this guard exists to prevent is a tag that no longer selects anything, and a
fixture-only suite would keep passing after a service is renamed in host_vars -- the
exact drift described in scripts/argparse-only-test territory (a path constant is only
pinned by code that opens the file).

Run: uv run pytest scripts/test_deploy_tags.py
"""

from __future__ import annotations

import textwrap

import pytest

import deploy_tags


@pytest.fixture
def host_vars(tmp_path):
    (tmp_path / "host_a.yml").write_text(
        textwrap.dedent(
            """\
            containers_list:
              - name: jellyfin
                platform: k8s
              - name: sonarr
                platform: k8s
            """
        )
    )
    (tmp_path / "host_b.yml").write_text(
        textwrap.dedent(
            """\
            containers_list:
              - name: dozzle
                platform: docker
            """
        )
    )
    # Excluded by the leading underscore, same as the role-existence test.
    (tmp_path / "_example.yml").write_text(
        "containers_list:\n  - name: not-a-real-service\n"
    )
    return tmp_path


def test_service_tags_span_both_hosts_and_platforms(host_vars):
    assert deploy_tags.service_tags(host_vars) == {"jellyfin", "sonarr", "dozzle"}


def test_example_host_vars_is_not_a_source_of_tags(host_vars):
    assert "not-a-real-service" not in deploy_tags.service_tags(host_vars)


def test_explicit_tags_override_the_name(tmp_path):
    (tmp_path / "host.yml").write_text(
        textwrap.dedent(
            """\
            containers_list:
              - name: n8n
                tags: [n8n, n8n-images]
            """
        )
    )
    # deploy.yml:116 uses `tags | default([name])`, so an override REPLACES the name.
    assert deploy_tags.service_tags(tmp_path) == {"n8n", "n8n-images"}


def test_block_tags_are_valid(host_vars):
    assert (
        deploy_tags.unknown_tags(["config", "deploy", "cron", "always"], host_vars)
        == []
    )


def test_unknown_tag_is_reported(host_vars):
    assert deploy_tags.unknown_tags(["jellifin"], host_vars) == ["jellifin"]


def test_known_and_unknown_together_reports_only_the_unknown(host_vars):
    assert deploy_tags.unknown_tags(["sonarr", "jellifin"], host_vars) == ["jellifin"]


def test_unknown_tags_are_deduplicated_and_ordered(host_vars):
    assert deploy_tags.unknown_tags(["zzz", "aaa", "zzz"], host_vars) == ["zzz", "aaa"]


def test_empty_tag_is_ignored(host_vars):
    # `--tags "sonarr,"` splits to an empty element; that is a stray comma, not a typo.
    assert deploy_tags.unknown_tags(["sonarr", ""], host_vars) == []


def test_suggestion_finds_the_near_miss(host_vars):
    assert "jellyfin" in deploy_tags.suggest("jellifin", host_vars)


def test_no_suggestion_when_nothing_is_close(host_vars):
    assert deploy_tags.suggest("qqqqqqqq", host_vars) == []


def test_validate_exits_zero_on_a_known_tag(capsys):
    # Against the real inventory: traefik must always be deployable by name, since the
    # k8s play depends on it being first in containers_list.
    assert deploy_tags.main(["validate", "traefik"]) == 0
    assert capsys.readouterr().err == ""


def test_validate_exits_two_and_explains_on_a_typo(capsys):
    assert deploy_tags.main(["validate", "jellifin"]) == 2
    err = capsys.readouterr().err
    assert "no service or block tag named 'jellifin'" in err
    assert "jellyfin" in err
    assert "Nothing was deployed" in err


def test_list_prints_the_real_tags(capsys):
    assert deploy_tags.main(["list"]) == 0
    printed = capsys.readouterr().out.split()
    assert "traefik" in printed
    assert "config" in printed
    # Sorted, so shell completion gets a stable order.
    assert printed == sorted(printed)


def test_real_inventory_has_no_empty_service_names():
    assert all(tag for tag in deploy_tags.service_tags())


# --------------------------------------------------------------------------------- describe


def test_service_records_carry_host_and_platform(host_vars):
    records = deploy_tags.service_records(host_vars)
    assert ("host_a", "k8s", "jellyfin") in records
    assert ("host_a", "k8s", "sonarr") in records
    # daniel-pi-style entries carry no `platform:` key at all — docker is the default,
    # not a third unlabelled state.
    assert ("host_b", "docker", "dozzle") in records


def test_describe_groups_by_host_and_flags_dry_run_unsupported(capsys):
    assert deploy_tags.main(["describe"]) == 0
    out = capsys.readouterr().out
    assert "daniel-box (k8s):" in out
    # crowdsec is dry-run-unsupported in the real group_vars/all.yml.
    assert "crowdsec  [dry-run: unsupported]" in out
    assert "block tags: config, cron, deploy" in out


def test_describe_does_not_change_lists_own_shape(capsys):
    # `list` stays flat/sorted (test_list_prints_the_real_tags above pins that). describe is
    # a separate view, not a reformat of it.
    deploy_tags.main(["list"])
    list_out = capsys.readouterr().out
    deploy_tags.main(["describe"])
    describe_out = capsys.readouterr().out
    assert list_out != describe_out


# ----------------------------------------------------------------------------------- changed


def test_changed_prints_tags_for_a_service_and_k8s_change(capsys, monkeypatch):
    monkeypatch.setattr(
        deploy_tags,
        "_git_diff_paths",
        lambda ref: [
            "ansible/roles/containers/dozzle/templates/docker-compose.yml.j2",
            "ansible/roles/k8s/jellyfin/templates/deployment.yaml.j2",
        ],
    )
    assert deploy_tags.main(["changed"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "dozzle,jellyfin"
    assert "dozzle, jellyfin" in captured.err


def test_changed_refuses_a_broad_change(capsys, monkeypatch):
    monkeypatch.setattr(
        deploy_tags,
        "_git_diff_paths",
        lambda ref: ["ansible/inventory/host_vars/daniel-box.yml"],
    )
    assert deploy_tags.main(["changed"]) == 3
    captured = capsys.readouterr()
    # Nothing but the tag list belongs on stdout — a refusal must not leak a stray tag line
    # a caller might mistake for one.
    assert captured.out == ""
    assert "refusing" in captured.err
    assert "ansible-playbook ansible/deploy.yml" in captured.err


def test_changed_reports_no_files_differ(capsys, monkeypatch):
    monkeypatch.setattr(deploy_tags, "_git_diff_paths", lambda ref: [])
    assert deploy_tags.main(["changed"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no files differ" in captured.err


def test_changed_warns_but_still_exits_zero_on_a_docs_only_change(capsys, monkeypatch):
    # tasks/-only change: not auto-deployed, not a tag, but must not be silently dropped.
    monkeypatch.setattr(
        deploy_tags,
        "_git_diff_paths",
        lambda ref: ["ansible/roles/containers/dozzle/tasks/main.yml"],
    )
    assert deploy_tags.main(["changed"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "structural change" in captured.err
    assert "dozzle" in captured.err


def test_changed_default_ref_is_origin_master():
    parser_ref = []

    def fake_git_diff(ref):
        parser_ref.append(ref)
        return []

    orig = deploy_tags._git_diff_paths
    deploy_tags._git_diff_paths = fake_git_diff
    try:
        deploy_tags.main(["changed"])
    finally:
        deploy_tags._git_diff_paths = orig
    assert parser_ref == ["origin/master"]
