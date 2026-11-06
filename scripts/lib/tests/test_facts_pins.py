import pytest

from lib.facts.citations import tracked_files
from lib.facts.pins import image_pins, pinned_keys, split_image_ref
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


def test_the_real_config_names_the_built_in_managers_pins():
    """#4158: no custom manager reads this file; the built-in github-actions manager bumps
    image-smoke's `uses:` refs."""
    keys = pinned_keys(REPO, ".github/workflows/image-smoke.yml")
    assert "jobs" in keys
    assert "on" not in keys


def test_the_built_in_ansible_manager_pins_an_image_in_a_tasks_file(tmp_path):
    """No tracked tasks file holds an `image:` line, so a fixture proves the selection."""
    tasks = tmp_path / "ansible" / "roles" / "demo" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        "- name: First\n  debug:\n- name: Second\n  vars:\n    image: demo:1\n"
    )
    (tmp_path / "renovate.json").write_text("{}")
    rel = "ansible/roles/demo/tasks/main.yml"
    assert pinned_keys(tmp_path, rel) == frozenset({"1"})


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


def test_the_real_tree_names_the_image_pins_it_must_find():
    """A role default, an inventory var and a Pi compose line; a built image pins nothing."""
    pins = image_pins(REPO, tracked_files(REPO))
    assert ("registry_k8s_image", "ansible/roles/k8s/registry/defaults/main.yml") in {
        p[1:] for p in pins["registry"]
    }
    assert pins["crowdsecurity/crowdsec"][0][1:] == (
        "crowdsec_k8s_image",
        "ansible/inventory/group_vars/all.yml",
    )
    assert (
        "image:",
        "hosts/components/pi-compose/services/alloy/compose.yaml.j2",
    ) in {p[1:] for p in pins["grafana/alloy"]}
    assert not [name for name in pins if "{{" in name or "code-server" in name]


@pytest.mark.parametrize(
    ("ref", "parts"),
    [
        ("registry:3.1.2@sha256:ab", ("registry", "3.1.2")),
        ("host:5000/team/img:1.0", ("host:5000/team/img", "1.0")),
        ("host:5000/team/img", None),
        ("t/m.py:LIMIT", ("t/m.py", "LIMIT")),
        ("org/app@sha256:ab", None),
    ],
)
def test_split_image_ref(ref, parts):
    assert split_image_ref(ref) == parts
