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
