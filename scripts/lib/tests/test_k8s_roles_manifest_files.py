"""`declared_manifest_files` reads every value shape a caller writes `manifests_files` in.

The offline render harnesses ask this which manifests a role's tasks name, so they can render
the shared default `k8s/manifests` would render for a basename the role ships no template for
(#2872). A shape it fails to read returns fewer names, the harness renders one manifest fewer,
and every guard built on the corpus passes with less coverage than yesterday.

Three shapes exist in the tree, and the third is the one a YAML load cannot reach: authelia,
freshrss and traefik build the list with a folded `>-` Jinja expression.

Run: uv run pytest scripts/lib/tests/test_k8s_roles_manifest_files.py
"""

from lib.k8s_roles import declared_manifest_files, shared_default_templates

# A role that takes the shared default Service and one that does not, so a fallback that
# stopped resolving (or started resolving for everyone) fails by name.
TAKES_THE_DEFAULT = "bazarr"
SHIPS_ITS_OWN = "registry"


def _role(tmp_path, name, tasks_body):
    role = tmp_path / name
    (role / "tasks").mkdir(parents=True)
    (role / "templates").mkdir()
    (role / "tasks" / "main.yml").write_text(tasks_body)
    return role


def test_an_inline_list_is_read(tmp_path):
    _role(
        tmp_path,
        "widget",
        "---\n- name: Deploy widget\n  vars:\n"
        "    manifests_files: [deployment.yaml, service.yaml]\n"
        "    manifests_rollout: widget\n",
    )
    assert declared_manifest_files("widget", tmp_path) == {
        "deployment.yaml",
        "service.yaml",
    }


def test_a_block_list_is_read(tmp_path):
    _role(
        tmp_path,
        "widget",
        "---\n- name: Deploy widget\n  vars:\n"
        "    manifests_files:\n      - deployment.yaml\n      - service.yaml\n"
        "    manifests_secret_files:\n      - secret.yaml\n",
    )
    assert declared_manifest_files("widget", tmp_path) == {
        "deployment.yaml",
        "service.yaml",
        "secret.yaml",
    }


def test_a_folded_jinja_expression_is_read(tmp_path):
    """authelia, freshrss and traefik write it this way; no YAML load resolves it to a list."""
    _role(
        tmp_path,
        "widget",
        "---\n- name: Deploy widget\n  vars:\n"
        "    manifests_files: >-\n"
        "      {{ ([] if widget_manage_claim | bool else ['pvc.yaml'])\n"
        "         + ['deployment.yaml', 'service.yaml'] }}\n"
        "    manifests_rollout: widget\n",
    )
    assert declared_manifest_files("widget", tmp_path) == {
        "pvc.yaml",
        "deployment.yaml",
        "service.yaml",
    }


def test_a_sibling_key_is_not_read_as_a_manifest(tmp_path):
    """The value region ends at the next key indented no deeper than `manifests_files:`."""
    _role(
        tmp_path,
        "widget",
        "---\n- name: Deploy widget\n  vars:\n"
        "    manifests_files:\n      - deployment.yaml\n"
        "    manifests_deferred_dir_name: widget-later\n"
        "  loop_control:\n    label: other.yaml\n",
    )
    assert declared_manifest_files("widget", tmp_path) == {"deployment.yaml"}


def test_a_role_with_no_tasks_file_declares_nothing(tmp_path):
    (tmp_path / "widget").mkdir()
    assert declared_manifest_files("widget", tmp_path) == set()


def test_the_shared_default_resolves_only_when_the_role_ships_no_template(tmp_path):
    role = _role(
        tmp_path,
        "widget",
        "---\n- name: Deploy widget\n  vars:\n"
        "    manifests_files: [deployment.yaml, service.yaml]\n",
    )
    assert [p.name for p in shared_default_templates("widget", tmp_path)] == [
        "service-default.yaml.j2"
    ]

    (role / "templates" / "service.yaml.j2").write_text("---\nkind: Service\n")
    assert shared_default_templates("widget", tmp_path) == [], (
        "a role's own template must win over the shared default — otherwise the fallback "
        "would render a second Service manifest over the one the role wrote."
    )


def test_a_role_that_names_no_service_gets_no_default(tmp_path):
    _role(
        tmp_path,
        "widget",
        "---\n- name: Deploy widget\n  vars:\n    manifests_files: [deployment.yaml]\n",
    )
    assert shared_default_templates("widget", tmp_path) == []


def test_the_real_tree_answers_both_ways():
    """Non-vacuity against the repo itself, not just synthetic roles."""
    assert "service.yaml" in declared_manifest_files(TAKES_THE_DEFAULT)
    assert [p.name for p in shared_default_templates(TAKES_THE_DEFAULT)] == [
        "service-default.yaml.j2"
    ]
    assert "service.yaml" in declared_manifest_files(SHIPS_ITS_OWN)
    assert shared_default_templates(SHIPS_ITS_OWN) == []
