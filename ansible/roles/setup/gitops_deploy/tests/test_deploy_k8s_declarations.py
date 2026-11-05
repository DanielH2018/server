"""Reading a k8s role's own declarations out of its defaults, without a YAML parser.

The deployer runs under `uv run --no-project` and cannot import yaml, so `declared_denylist`
is a regex over source text, biased toward denied: an absent, indented, duplicated or
unparseable declaration counts as denied. It moved into `deploy_phases` beside its one caller,
`reconcile_denylist`, when D6.12 deleted `deploy_k8s.py`, and `k8s_role_paths` moved into
`deploy_io` beside its one caller, `k8s_declarations_at`. D6.2a deletes both.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_k8s_declarations.py

from deployer_lib.deploy_io import k8s_role_paths
from deployer_lib.deploy_phases import SHARED_K8S_ROLES, declared_denylist


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
