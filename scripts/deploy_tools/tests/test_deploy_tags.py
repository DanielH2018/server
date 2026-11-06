#!/usr/bin/env python3
"""Tests for the deploy --tags guard.

Half of these run against the REAL inventory rather than a fixture. That is deliberate:
the failure this guard exists to prevent is a tag that no longer selects anything, and a
fixture-only suite would keep passing after a service is renamed in host_vars -- the
exact drift described in scripts/argparse-only-test territory (a path constant is only
pinned by code that opens the file).

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tags.py
"""

import textwrap
from pathlib import Path

import pytest


from deploy_tools.deploy_lib import tags as deploy_tags
from lib import render_guard


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


def test_service_records_carry_host_and_platform(host_vars):
    records = deploy_tags.service_records(host_vars)
    assert ("host_a", "k8s", "jellyfin") in records
    assert ("host_a", "k8s", "sonarr") in records
    # daniel-pi-style entries carry no `platform:` key at all — docker is the default,
    # not a third unlabelled state.
    assert ("host_b", "docker", "dozzle") in records


def test_describe_groups_by_host_and_platform(capsys):
    assert deploy_tags.main(["describe"]) == 0
    out = capsys.readouterr().out
    assert "daniel-box (k8s):" in out
    assert "block tags: config, cron, deploy" in out


def test_describe_does_not_change_lists_own_shape(capsys):
    # `list` stays flat/sorted (test_list_prints_the_real_tags above pins that). describe is
    # a separate view, not a reformat of it.
    deploy_tags.main(["list"])
    list_out = capsys.readouterr().out
    deploy_tags.main(["describe"])
    describe_out = capsys.readouterr().out
    assert list_out != describe_out


def test_split_shared_roles_separates_an_undeclared_role(host_vars):
    deployable, shared = deploy_tags.split_shared_roles(
        {"sonarr", "jellyfin", "manifests"}, host_vars
    )
    assert deployable == ["jellyfin", "sonarr"]
    assert shared == ["manifests"]


def test_split_shared_roles_calls_nothing_shared_when_all_are_declared(host_vars):
    """The reject half.

    A splitter that pushed everything into `shared` would derive no tags at all and turn every
    landing into a full-deploy instruction.
    """
    deployable, shared = deploy_tags.split_shared_roles(
        {"sonarr", "jellyfin"}, host_vars
    )
    assert deployable == ["jellyfin", "sonarr"]
    assert shared == []


def test_changed_derives_no_tag_for_a_k8s_role_or_a_shared_one(capsys, monkeypatch):
    """`promote` applies both, and `deploy.sh` would refuse either tag."""
    monkeypatch.setattr(
        deploy_tags,
        "_git_diff_paths",
        lambda ref, cwd=None: [
            "ansible/roles/k8s/manifests/tasks/main.yml",
            "ansible/roles/k8s/jellyfin/defaults/main.yml",
        ],
    )
    assert deploy_tags.main(["changed"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "left out `jellyfin` is applied by Flux" in captured.err
    assert "ansible/deploy.yml" not in captured.err


def test_changed_on_a_shared_role_alone_exits_0_with_no_tag(capsys, monkeypatch):
    """`derive_changed_tags` reports an empty list as nothing to deploy, so no false green."""
    monkeypatch.setattr(
        deploy_tags,
        "_git_diff_paths",
        lambda ref, cwd=None: ["ansible/roles/k8s/manifests/tasks/main.yml"],
    )
    assert deploy_tags.main(["changed"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no deployable service changed" in captured.err


def test_changed_refuses_a_broad_change(capsys, monkeypatch):
    monkeypatch.setattr(
        deploy_tags,
        "_git_diff_paths",
        lambda ref, cwd=None: ["ansible/inventory/host_vars/daniel-box.yml"],
    )
    assert deploy_tags.main(["changed"]) == 3
    captured = capsys.readouterr()
    # Nothing but the tag list belongs on stdout — a refusal must not leak a stray tag line
    # a caller might mistake for one.
    assert captured.out == ""
    assert "deploy.sh: error: --changed: refusing" in captured.err
    assert "`promote` and Flux apply the cluster half" in captured.err
    assert "homelab-daniel-pi" in captured.err
    assert "ansible/deploy.yml" not in captured.err
    assert "target=daniel-pi" not in captured.err


def test_changed_reports_no_files_differ(capsys, monkeypatch):
    monkeypatch.setattr(deploy_tags, "_git_diff_paths", lambda ref, cwd=None: [])
    assert deploy_tags.main(["changed"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no files differ" in captured.err


def test_changed_default_ref_is_origin_master():
    parser_ref = []

    def fake_git_diff(ref: str, cwd: Path = deploy_tags.REPO) -> list[str]:
        parser_ref.append(ref)
        return []

    deploy_tags.main(["changed"], git_diff=fake_git_diff)
    assert parser_ref == ["origin/master"]


def test_tag_platforms_reads_the_declaring_platforms(host_vars):
    assert deploy_tags.tag_platforms("dozzle", host_vars) == {"docker"}
    assert deploy_tags.tag_platforms("jellyfin", host_vars) == {"k8s"}
    assert deploy_tags.tag_platforms("config", host_vars) == set()


def test_real_inventory_gives_the_packaged_pi_services_no_deploy_tag():
    """Non-vacuity against the live inventory: `alloy` left `containers_list` in H5.8, and
    `wg-easy` left the Pi's in H5.9.

    The Pi's package deploys both, so the Pi declares neither tag and `deploy.sh --tags alloy`
    selects nothing. The cluster's `wg-easy` still resolves, which proves the lookup reads the
    real files.
    """
    assert deploy_tags.tag_platforms("alloy") == set()
    assert deploy_tags.tag_platforms("wg-easy") == {"k8s"}


def test_no_host_declaring_a_tag_is_one_land_sh_never_deploys_to():
    """A host added to the constant without being added to the inventory, or the reverse, is
    the drift to catch."""
    declaring = {host for host, _platform, _tag in deploy_tags.service_records()}
    assert declaring, "no host declares any tag — this test would pass over nothing"
    assert not (declaring & render_guard.HOSTS_LAND_SH_NEVER_DEPLOYS), (
        f"{sorted(declaring & render_guard.HOSTS_LAND_SH_NEVER_DEPLOYS)} declare tags that "
        "land.sh must not deploy"
    )


def test_deploy_logic_importers_do_not_grow_sys_path_per_call():
    """The deploy_logic importers must not grow sys.path.

    They must not `sys.path.insert` the role dir on every call and never remove it. The
    entry is put there once at module scope; calling them twice must leave the count exactly
    where it was.
    """
    import sys

    entry = str(deploy_tags.DEPLOY_LOGIC_DIR)
    before = sys.path.count(entry)
    assert before >= 1, "module import no longer puts DEPLOY_LOGIC_DIR on sys.path"
    for _ in range(2):
        deploy_tags._load_deploy_logic()
        deploy_tags.comment_only_paths([], "HEAD", "HEAD")
    assert sys.path.count(entry) == before


# -- a comment-only k8s defaults edit derives no landing tag (#4460) ----------------------
#
# The tick drops this edit as no change, so a landing that still derived `--tags jellyfin`
# ran a no-op deploy for it. `comment_only_paths` is the quiet set land.sh hands `derive`.

_JELLYFIN_DEFAULTS = "ansible/roles/k8s/jellyfin/defaults/main.yml"


@pytest.mark.parametrize(
    "after, quiet, tags",
    [
        ("# repointed\njellyfin_k8s_size: 8Gi\n", {_JELLYFIN_DEFAULTS}, []),
        ("# design record\njellyfin_k8s_size: 16Gi\n", set(), ["jellyfin"]),
    ],
    ids=["comment-only", "value-edit"],
)
def test_a_landing_derives_no_tag_for_a_comment_only_k8s_defaults_edit(
    after, quiet, tags
):
    from deploy_tools.land_lib import land_tags

    texts = {
        ("old", _JELLYFIN_DEFAULTS): "# design record\njellyfin_k8s_size: 8Gi\n",
        ("new", _JELLYFIN_DEFAULTS): after,
    }
    found = deploy_tags.comment_only_paths(
        [_JELLYFIN_DEFAULTS], "old", "new", lambda ref, path: texts[(ref, path)]
    )
    assert found == quiet
    derived = land_tags.derive([_JELLYFIN_DEFAULTS], 1, {"jellyfin"}, quiet=found)
    assert derived.tags == tags


# -- validate --at: containers_list as a git ref declares it ----------------------------
#
# `deploy.sh --at <sha>` renders a snapshot of <sha>, so the tags it deploys are the ones
# <sha> declares. A PR that adds a role and its containers_list entry together declares its
# tag in NO working tree until the tick fast-forwards, so the first landing of a new
# service would read as a tag miss (`land_lib.tools.declared_tags_at` fixes the same read
# one layer up).


def test_known_tags_at_a_ref_still_carries_the_block_tags(host_vars):
    """CLEAN half, against this checkout's own HEAD: a real ref and a real containers_list.

    The read itself and its damage rule are `render_guard.service_tags_at_or_none`, tested in
    scripts/lib/tests/test_render_guard.py; what is asserted here is that `known_tags` routes
    through it and still adds the block tags a service list does not carry.
    """
    at_head = deploy_tags.known_tags(host_vars, at="HEAD")
    assert {"config", "deploy", "always"} <= at_head
    # The ref was read, not `host_vars`: traefik is declared in this checkout's inventory and
    # in no fixture, and the two sets differ.
    assert "traefik" in at_head
    assert at_head != deploy_tags.known_tags(host_vars)


def test_an_unreadable_ref_falls_back_to_the_working_tree(host_vars):
    """REJECTING half: an empty read is damage, never evidence -- `set()` refuses every tag.

    `host_vars` is the fixture inventory, so the fallback is visible: it answers `jellyfin`,
    which this checkout's real containers_list does not declare.
    """
    fell_back = deploy_tags.known_tags(host_vars, at="deadbeefdeadbeef")
    assert fell_back == deploy_tags.known_tags(host_vars)
    assert "jellyfin" in fell_back
