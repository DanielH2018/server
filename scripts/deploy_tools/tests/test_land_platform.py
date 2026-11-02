"""`land_platform.k8s_only_tags`: every tag a PR's changed paths derive.

No tag is declared on two platforms, so a derived tag needs no proof of which platform it
belongs to. The last test before `diff_range`'s holds that premise against the live tree.

`test_land_tags_landing_hosts_at.py` owns the routing rule this feeds, and
`test_land_deploy_platform_routing.py` owns whether the landing hands it over.

Run: uv run pytest scripts/deploy_tools/tests/test_land_platform.py
"""

from deploy_tools.deploy_lib import tags as deploy_tags
from deploy_tools.land_lib import land_platform
from lib.git_testing import commit, git_out, init_repo
from deploy_tools.deploy_lib.tags import service_records
from lib.render_guard import HOST_VARS
from lib.repo_paths import REPO

_K8S_TEMPLATE = "ansible/roles/k8s/wg-easy/templates/deployment.yaml.j2"
# The Pi's Compose wg-easy before H5.12 deleted `roles/containers/`.
_DOCKER_TEMPLATE = "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"

_DECLARED = {"wg-easy", "sonarr", "alloy"}


def test_each_derived_tag_is_returned_once_and_sorted():
    assert land_platform.k8s_only_tags(
        [_K8S_TEMPLATE, "ansible/roles/k8s/sonarr/tasks/main.yml", _K8S_TEMPLATE],
        _DECLARED,
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


def test_the_live_tree_declares_no_tag_on_two_platforms():
    """The premise `k8s_only_tags` returns every derived tag on.

    A tag declared on two platforms again would need its platform proved from the paths, so
    this fails rather than let the landing route that tag to its k8s entry alone.
    """
    platforms: dict[str, set[str]] = {}
    for _host, platform, tag in service_records(HOST_VARS):
        platforms.setdefault(tag, set()).add(platform)
    assert platforms, "containers_list declares nothing, so this proves nothing"
    assert {t: p for t, p in platforms.items() if len(p) > 1} == {}
    assert platforms["wg-easy"] == {"k8s"}
    assert (REPO / _K8S_TEMPLATE).exists(), _K8S_TEMPLATE
    assert not (REPO / _DOCKER_TEMPLATE).exists(), _DOCKER_TEMPLATE


def test_diff_range_is_the_range_deploy_tags_changed_reads(tmp_path):
    """Non-vacuity for the fallback derivation: the landing reads this range's paths to
    name its k8s-only tags, while `deploy_tags.changed` derives the tags from its own spelling.
    A two-dot range would name the tags of a different file set with every case above still
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
