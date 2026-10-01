"""The inventory readers every generator and validator shares.

``load_yaml`` coerces a non-mapping file to ``{}`` and must keep doing so, or a host_vars
file whose top level is a list reaches a ``.get`` several calls later.
"""

from pathlib import Path

from render_guard import (
    HOST_VARS,
    HOST_VARS_IN_TREE,
    REPO,
    host_files,
    hosts_for_tags,
    load_yaml,
    service_records_at_or_none,
    service_tags_at_or_none,
)
from repo_paths import ANSIBLE, INVENTORY, ROLES
from lib.git_testing import commit, init_repo

# One commit declaring a service, and one adding a second. `deploy.sh --at <sha>` validates
# its tags against the commit it renders, so the answer must move with the ref.
_WITHOUT = "containers_list:\n  - name: sonarr\n    platform: k8s\n"
_WITH = _WITHOUT + "  - name: newsvc\n    platform: k8s\n"


def _tags_repo(tmp_path: Path, *texts: str | dict[str, str]) -> list[str]:
    """Commit each `texts` entry as the host_vars file in `tmp_path`; the shas, in order.

    A str is `daniel-box.yml`'s text; a dict is `{file name: text}` for a commit that writes
    several hosts at once.

    `lib.git_testing` scrubs every ``GIT_*`` variable: under a prek hook an inherited
    ``GIT_DIR`` beats ``cwd``, and these commits would land in the real repository.
    """
    init_repo(tmp_path)
    host_vars = tmp_path / HOST_VARS_IN_TREE
    host_vars.mkdir(parents=True)
    shas = []
    for n, text in enumerate(texts):
        files = text if isinstance(text, dict) else {"daniel-box.yml": text}
        shas.append(
            commit(
                tmp_path,
                f"c{n}",
                **{
                    f"{HOST_VARS_IN_TREE}/{name}": content
                    for name, content in files.items()
                },
            )
        )
    return shas


def test_service_tags_at_a_ref_sees_what_that_commit_declares(tmp_path):
    """CLEAN half: a role and its containers_list entry added together are declared at it."""
    shas = _tags_repo(tmp_path, _WITHOUT, _WITH)
    assert service_tags_at_or_none(shas[1], tmp_path) == {"sonarr", "newsvc"}


def test_the_same_tag_is_absent_at_the_commit_before_it(tmp_path):
    """The answer has to MOVE with the ref, or reading at one proves nothing."""
    shas = _tags_repo(tmp_path, _WITHOUT, _WITH)
    assert service_tags_at_or_none(shas[0], tmp_path) == {"sonarr"}


def test_an_unreadable_ref_is_none_rather_than_an_empty_set(tmp_path):
    """REJECTING half: `set()` says no service is declared anywhere, which refuses every tag.

    None is what sends the caller back to the tree it can read.
    """
    _tags_repo(tmp_path, _WITHOUT)
    assert service_tags_at_or_none("deadbeefdeadbeefdeadbeef", tmp_path) is None


def test_host_vars_that_do_not_parse_at_the_ref_are_none_too(tmp_path):
    """The second damage mode, and the one the read actually meets: unparseable YAML.

    `service_tags_at` parses what it fetches from the ref, so a `yaml.YAMLError` escaping here
    turns `deploy_tags.py validate --at <sha>` into a traceback, which `deploy.sh` reports as
    its tag-miss exit -- "a --tags value matched no service", for a file that merely does not
    parse.
    """
    shas = _tags_repo(tmp_path, _WITHOUT, "containers_list:\n  - name: [unclosed\n")
    assert service_tags_at_or_none(shas[1], tmp_path) is None


def test_service_records_at_a_ref_keep_the_declaring_host(tmp_path):
    """CLEAN half: the host survives the read, so a Pi entry added at the
    commit routes to daniel-pi through the same rule the working-tree read uses."""
    shas = _tags_repo(
        tmp_path,
        _WITHOUT,
        {
            "daniel-pi.yml": "containers_list:\n  - name: newpi\n",
            "_example.yml": "x: 1\n",
        },
    )
    records = service_records_at_or_none(shas[1], tmp_path)
    assert records == [
        ("daniel-box", "k8s", "sonarr"),
        ("daniel-pi", "docker", "newpi"),
    ]
    assert hosts_for_tags(["newpi", "sonarr", "config"], records) == {
        "daniel-box": ["sonarr"],
        "daniel-pi": ["newpi"],
    }
    # The same tag is declared on NO host at the commit before it.
    assert (
        hosts_for_tags(["newpi"], service_records_at_or_none(shas[0], tmp_path)) == {}
    )


def test_unreadable_service_records_are_none_rather_than_an_empty_list(tmp_path):
    """REJECTING half: `[]` would route every tag to no host, which is a local deploy."""
    _tags_repo(tmp_path, _WITHOUT)
    assert service_records_at_or_none("deadbeefdeadbeefdeadbeef", tmp_path) is None


def test_load_yaml_returns_a_mapping(tmp_path):
    path = tmp_path / "a.yml"
    path.write_text("key: value\n")
    assert load_yaml(path) == {"key": "value"}


def test_load_yaml_returns_empty_for_a_missing_file(tmp_path):
    assert load_yaml(tmp_path / "absent.yml") == {}


def test_load_yaml_returns_empty_for_an_empty_file(tmp_path):
    path = tmp_path / "empty.yml"
    path.write_text("")
    assert load_yaml(path) == {}


def test_load_yaml_returns_empty_for_a_non_mapping(tmp_path):
    path = tmp_path / "list.yml"
    path.write_text("- one\n- two\n")
    assert load_yaml(path) == {}


def test_host_files_skips_the_example_template(tmp_path):
    (tmp_path / "_example.yml").write_text("server_ip: 1.1.1.1\n")
    (tmp_path / "daniel-box.yml").write_text("server_ip: 2.2.2.2\n")
    assert [p.name for p in host_files(tmp_path)] == ["daniel-box.yml"]


def test_anchors_resolve_to_the_real_tree():
    """A wrong ``parents[N]`` reads as an empty inventory, not an error."""
    assert (REPO / "pyproject.toml").is_file()
    assert (ANSIBLE / "deploy.yml").is_file()
    assert (INVENTORY / "hosts.ini").is_file()
    assert (HOST_VARS / "daniel-box.yml").is_file()
    assert (ROLES / "k8s").is_dir()


def test_render_guard_re_exports_the_same_anchor_objects():
    from repo_paths import REPO as PATHS_REPO

    assert REPO == PATHS_REPO
    assert isinstance(REPO, Path)


def test_every_public_name_is_listed_in_all():
    """`__all__` drifted behind three helpers this module gained.

    Derived from the module rather than compared to a copied list: a hand-written expected
    list is the same drift one indirection away. Sorted, because the list is maintained in
    that order and an append at the end reads as correct until someone bisects it.
    """
    import types

    import render_guard

    public = {
        name
        for name, value in vars(render_guard).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", "render_guard") == "render_guard"
    }
    assert public - set(render_guard.__all__) == set(), sorted(
        public - set(render_guard.__all__)
    )
    assert render_guard.__all__ == sorted(render_guard.__all__)
    for name in render_guard.__all__:
        assert hasattr(render_guard, name), name
