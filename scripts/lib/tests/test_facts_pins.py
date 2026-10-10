from lib.facts.pins import pinned_keys
from lib.repo_paths import REPO


def test_the_real_config_names_the_pins_it_must_find():
    """Both pin shapes Renovate edits in YAML: an `_image:` ref and a `# renovate:` version."""
    assert "crowdsec_k8s_image" in pinned_keys(
        REPO, "ansible/inventory/group_vars/all.yml"
    )
    assert "code_server_k8s_node_version" in pinned_keys(
        REPO, "ansible/roles/k8s/code-server/defaults/main.yml"
    )


def test_a_key_no_manager_captures_is_not_a_pin():
    assert "crowdsec_k8s_sidecar_agents" not in pinned_keys(
        REPO, "ansible/inventory/group_vars/all.yml"
    )


def test_the_real_config_names_the_built_in_github_actions_pins():
    """#4158: the built-in github-actions manager bumps `uses:` and `runs-on:` under `jobs`."""
    keys = pinned_keys(REPO, ".github/workflows/ci.yml")
    assert "jobs" in keys
    assert "on" not in keys


def test_a_disabled_built_in_manager_pins_nothing(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n"
    )
    (tmp_path / "renovate.json").write_text('{"github-actions": {"enabled": false}}')
    assert pinned_keys(tmp_path, ".github/workflows/ci.yml") == frozenset()
    (tmp_path / "renovate.json").write_text('{"enabledManagers": ["pep621"]}')
    assert pinned_keys(tmp_path, ".github/workflows/ci.yml") == frozenset()
    (tmp_path / "renovate.json").write_text("{}")
    assert pinned_keys(tmp_path, ".github/workflows/ci.yml") == {"jobs"}
