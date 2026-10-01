"""`land_platform.k8s_only_tags`: which derived tags a PR's paths prove are k3s (#2730).

#2718 restricted the tags the k8s caller graph named to a `platform: k8s` entry. A tag a
changed PATH named kept routing to both hosts, so a PR touching only
`ansible/roles/k8s/wg-easy/templates/` still ran `deploy.sh -e target=daniel-pi --tags
wg-easy`. The rejecting halves here are the more important ones: narrowing host routing is
how issue #929 (a landing that read `settled` while the Pi ran old code) comes back.

`test_land_tags_landing_hosts_at.py` owns the routing rule this feeds, and
`test_land_deploy_platform_routing.py` owns whether the landing hands it over.

Run: uv run pytest scripts/deploy_tools/tests/test_land_platform.py
"""

import deploy_tags
import land_platform
from lib.git_testing import commit, git_out, init_repo
import land_tags
from deploy_tags import service_records
from lib.render_guard import HOST_VARS
from lib.repo_paths import REPO

# Both trees really declare `wg-easy` -- a `platform: k8s` entry on daniel-box and the Pi's
# Compose service -- which is what makes it the one tag this filter exists for.
_K8S_TEMPLATE = "ansible/roles/k8s/wg-easy/templates/deployment.yaml.j2"
_DOCKER_TEMPLATE = "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"

_DECLARED = {"wg-easy", "sonarr", "alloy"}


def test_a_k8s_only_path_set_proves_the_tag_is_k3s():
    """CLEAN half: every path naming `wg-easy` sits under the k8s tree, so the landing may
    route it to the `platform: k8s` entry alone."""
    assert land_platform.k8s_only_tags([_K8S_TEMPLATE], _DECLARED) == ["wg-easy"]


def test_a_tag_named_in_both_trees_is_not_restricted():
    """REJECTING half, and the one that matters: a PR changing both wg-easy services changes
    the Pi's too, so restricting the tag would leave the Pi running the old container."""
    assert (
        land_platform.k8s_only_tags([_K8S_TEMPLATE, _DOCKER_TEMPLATE], _DECLARED) == []
    )


def test_a_docker_only_path_set_is_not_restricted_either():
    """The restriction is `platform: k8s`, so a Compose path must never land in it."""
    assert land_platform.k8s_only_tags([_DOCKER_TEMPLATE], _DECLARED) == []


def test_each_tag_is_answered_on_its_own_paths():
    """Per tag, not per PR: a PR touching one tree for one service and the other for another
    restricts the k8s one and leaves the Pi's alone."""
    docker = "ansible/roles/containers/alloy/templates/docker-compose.yml.j2"
    assert land_platform.k8s_only_tags(
        [_K8S_TEMPLATE, "ansible/roles/k8s/sonarr/tasks/main.yml", docker], _DECLARED
    ) == ["sonarr", "wg-easy"]


def test_a_path_that_names_no_tag_restricts_nothing():
    """`tag_for` is the one mapper for this: a `.md`, a role's own `tests/`, an unregistered
    role and a path outside the role trees all drop out of the derivation, so none of them can
    put a tag into the restriction."""
    assert (
        land_platform.k8s_only_tags(
            [
                "ansible/roles/k8s/wg-easy/CLAUDE.md",
                "ansible/roles/k8s/wg-easy/tests/test_x.py",
                "ansible/roles/k8s/manifests/tasks/main.yml",
                "ansible/inventory/host_vars/daniel-box.yml",
            ],
            _DECLARED,
        )
        == []
    )


def test_an_undeclared_tag_never_reaches_the_restriction():
    """`declared` is the containers_list read at the merge commit, and a role with no entry has
    no tag to restrict -- naming one in `--tags` makes deploy.sh refuse the whole list."""
    assert land_platform.k8s_only_tags([_K8S_TEMPLATE], set()) == []


def test_the_tree_prefix_is_the_one_land_tags_matches_on():
    """Non-vacuity: this module tests a path against a literal prefix while `tag_for` maps the
    same path through `deploy_changes.role_of`. A tree renamed under one and not the other
    would leave this returning tags that prove nothing, with every test above still green."""
    assert land_tags.role_of(f"{land_platform.K8S_TREE}wg-easy/x.j2") == (
        "k8s",
        "wg-easy",
        "",
    )
    assert land_tags.role_for(_K8S_TEMPLATE) == "wg-easy"
    assert land_tags.role_for(_DOCKER_TEMPLATE) == "wg-easy"


def test_the_live_tree_declares_wg_easy_on_two_platforms():
    """The fixture is real, not synthetic: if the repo held `wg-easy` on one platform only,
    every case above would pass while covering a shape nothing here has.

    It is the only such tag, 1 of 62 on 2026-09-27, so this fails the day the Pi's wg-easy
    retires. Repoint the fixture at whatever tag is two-platform by then; if none is, the
    restriction covers nothing live and these tests go with it.
    """
    platforms = {
        platform
        for _host, platform, tag in service_records(HOST_VARS)
        if tag == "wg-easy"
    }
    assert platforms == {"k8s", "docker"}
    for path in (_K8S_TEMPLATE, _DOCKER_TEMPLATE):
        assert (REPO / path).exists(), path


def test_diff_range_is_the_range_deploy_tags_changed_reads(tmp_path):
    """Non-vacuity for the fallback derivation (#2738): the landing reads this range's paths to
    prove a platform, while `deploy_tags.changed` derives the tags from its own spelling. A
    two-dot range would prove the platform of a different file set with every case above still
    green, so this holds the two against each other on a history where they disagree -- the
    `other` branch carries a commit HEAD does not, and only three dots exclude it."""

    def at(name: str) -> None:
        commit(tmp_path, name, **{name: name})

    init_repo(tmp_path)
    at("base")
    git_out(tmp_path, "checkout", "-q", "-b", "other")
    at("theirs")
    git_out(tmp_path, "checkout", "-q", "master")
    at("ours")

    assert deploy_tags._git_diff_paths("other", tmp_path) == ["ours"]
    assert (
        git_out(tmp_path, "diff", "--name-only", land_platform.diff_range("other"))
        == "ours"
    )
