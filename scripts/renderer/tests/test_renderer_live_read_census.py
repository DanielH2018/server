"""R3's live-read census: no name a k8s-play task sets reaches a template the render reads.

`_live_read_census.py` is the census. The real-tree tests hold the tree clean and require the
named members of R3's *The census*. Each fixture test builds one role, as R3's *Red-proofs*
lists the cases.

Run: uv run pytest scripts/renderer/tests/test_renderer_live_read_census.py
"""

from pathlib import Path

import pytest
import yaml
from _live_read_census import (
    K8S_PLAY,
    ROLE_LOOP,
    Census,
    CensusError,
    Tree,
    census,
    real_census,
)

from lib.repo_paths import K8S_ROLES

# What a fixture's renderer derives: one secret key and the play's containers_list view.
FIXTURE_DERIVED = frozenset({"alpha_token", "containers_list"})


def test_the_tree_feeds_no_live_read_to_a_rendered_template():
    assert real_census().offenders() == []


# One name each task file sets, for the files the walk must enter: the play's preamble, the
# two helper roles every k8s role includes, and the post_tasks.
WALKED = {
    "ansible_become_password": "pre_tasks/load_secrets.yml",
    "k8s_built_image_tags": "roles/k8s/image-builder/tasks/main.yml",
    "manifests_render": "roles/k8s/manifests/tasks/main.yml",
    "k8s_image_drift_pods": "post_tasks/k8s_image_drift_gate.yml",
}


def test_the_task_walk_enters_every_file_the_play_reaches():
    setters = real_census().set_by_tasks
    missing = {
        name: path
        for name, path in WALKED.items()
        if path not in setters.get(name, set())
    }
    assert missing == {}


def test_the_census_finds_its_named_members():
    found = real_census()
    # A task sets it, a template reads it, and the renderer derives it.
    assert "kuma_declarations" in found.set_by_tasks.keys() & found.derived
    assert "kuma_declarations" in found.reached
    # Templates reach it only through role defaults, which proves the definition walk.
    readers = found.reached["k8s_built_image_tags"]
    assert "k8s_built_image_tags" in found.set_by_tasks
    assert readers and all("/defaults/" in reader for reader in readers)
    # The two `vars` lookup sources.
    assert {
        "monitor_bridge_disk_push_token",
        "terraria_k8s_replicas",
    } <= found.vars_names
    assert "terraria_k8s_replicas" in found.reached
    assert (
        "pipe in roles/k8s/homepage/templates/icons-configmap.yaml.j2" in found.accepted
    )


def test_the_template_set_holds_the_build_context_and_not_the_build_job():
    builder = K8S_ROLES / "image-builder" / "templates"
    templates = real_census().templates
    assert builder / "context-configmap.yaml.j2" in templates
    assert builder / "build-job.yaml.j2" not in templates


def _census(
    tmp_path: Path,
    templates: dict[str, str],
    tasks: str = "[]",
    defaults: str = "{}",
    files: dict[str, str] | None = None,
) -> Census:
    """The census over a tree with one role, `widget`, that the k8s play's batch loop runs."""
    ansible = tmp_path / "ansible"
    role = ansible / "roles" / "k8s" / "widget"
    play = [
        {
            "name": K8S_PLAY,
            "hosts": "all",
            "tasks": [{"ansible.builtin.include_role": {"name": ROLE_LOOP}}],
        }
    ]
    written = {
        ansible / "deploy.yml": yaml.safe_dump(play),
        ansible / "inventory" / "group_vars" / "all.yml": "{}\n",
        ansible / "inventory" / "host_vars" / "daniel-box.yml": "{}\n",
        role / "tasks" / "main.yml": tasks,
        role / "defaults" / "main.yml": defaults,
        **{role / "templates" / name: text for name, text in templates.items()},
        **{role / "files" / name: text for name, text in (files or {}).items()},
    }
    for path, text in written.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "shared").mkdir()
    tree = Tree(ansible=ansible, shared=tmp_path / "shared")
    return census(tree, ["widget"], FIXTURE_DERIVED, {"widget": {}})


def _flagged(found: Census) -> set[str]:
    return {offender.split(":")[0] for offender in found.offenders()}


SLURP_TASKS = """
- name: Read the peer's host key
  ansible.builtin.slurp:
    src: /etc/ssh/ssh_host_ed25519_key.pub
  register: widget_hostkey
- name: Compose the pin line
  ansible.builtin.set_fact:
    widget_line: "{{ widget_hostkey.content | b64decode }}"
"""


def test_a_slurp_that_feeds_a_template_through_a_set_fact_is_flagged(tmp_path):
    found = _census(
        tmp_path, {"secret.yaml.j2": "known_hosts: {{ widget_line }}\n"}, SLURP_TASKS
    )
    assert _flagged(found) == {"widget_hostkey", "widget_line"}


def test_a_register_that_reaches_a_template_through_a_default_is_flagged(tmp_path):
    found = _census(
        tmp_path,
        {"deployment.yaml.j2": "image: {{ widget_image }}\n"},
        "- ansible.builtin.command: date\n  register: widget_read\n",
        'widget_image: "registry/widget@{{ widget_read.stdout }}"\n',
    )
    assert _flagged(found) == {"widget_read"}


def test_a_set_fact_that_shadows_a_default_a_template_reads_is_flagged(tmp_path):
    found = _census(
        tmp_path,
        {"deployment.yaml.j2": "port: {{ widget_port }}\n"},
        '- ansible.builtin.set_fact:\n    widget_port: "{{ 9090 }}"\n',
        "widget_port: 8080\n",
    )
    assert _flagged(found) == {"widget_port"}


def test_a_role_reading_only_a_defaults_value_is_clean(tmp_path):
    found = _census(
        tmp_path,
        {"deployment.yaml.j2": "port: {{ widget_port }}\n"},
        "- ansible.builtin.command: date\n  register: widget_read\n",
        "widget_port: 8080\n",
    )
    assert found.offenders() == []
    assert "widget_port" in found.reached


def test_a_set_fact_read_by_a_one_off_job_template_is_clean(tmp_path):
    found = _census(
        tmp_path, {"migrate-job.yaml.j2": "line: {{ widget_line }}\n"}, SLURP_TASKS
    )
    assert found.offenders() == []


def test_the_same_read_in_a_deployment_template_is_flagged(tmp_path):
    found = _census(
        tmp_path, {"deployment.yaml.j2": "line: {{ widget_line }}\n"}, SLURP_TASKS
    )
    assert "widget_line" in _flagged(found)


ICON = "{{ lookup('pipe', 'base64 -w0 ' ~ playbook_dir ~ '/roles/k8s/widget/files/a.png') }}"


def test_a_base64_pipe_lookup_is_clean(tmp_path):
    found = _census(tmp_path, {"icons-configmap.yaml.j2": f"icon: {ICON}\n"})
    assert found.offenders() == []
    assert found.accepted == [
        "pipe in roles/k8s/widget/templates/icons-configmap.yaml.j2"
    ]


def test_a_pipe_lookup_with_another_command_is_flagged(tmp_path):
    found = _census(
        tmp_path, {"configmap.yaml.j2": "host: {{ lookup('pipe', 'hostname') }}\n"}
    )
    assert len(found.problems) == 1
    assert "lookup('pipe')" in found.problems[0]


CHECK_LOOP = (
    "{% for check in lookup('file', playbook_dir ~ '/roles/k8s/widget/files/table.py')"
    " | py_table('CHECKS') %}\n"
    "  {{ check.name }}: {{ lookup('vars', check.token) }}\n"
    "{% endfor %}\n"
)
REGISTER_TASKS = "- ansible.builtin.command: date\n  register: widget_read\n"


def _table(token: str) -> dict[str, str]:
    return {"table.py": f'CHECKS = (Check(name="disk", token="{token}"),)\n'}


def test_a_vars_lookup_whose_table_names_a_derived_key_is_clean(tmp_path):
    found = _census(
        tmp_path,
        {"env-secret.yaml.j2": CHECK_LOOP},
        REGISTER_TASKS,
        files=_table("alpha_token"),
    )
    assert found.offenders() == []
    assert found.vars_names == {"alpha_token"}


def test_a_vars_lookup_whose_table_names_a_task_set_fact_is_flagged(tmp_path):
    found = _census(
        tmp_path,
        {"env-secret.yaml.j2": CHECK_LOOP},
        REGISTER_TASKS,
        files=_table("widget_read"),
    )
    assert _flagged(found) == {"widget_read"}


def test_a_vars_lookup_over_an_argument_it_cannot_resolve_is_flagged(tmp_path):
    found = _census(
        tmp_path, {"configmap.yaml.j2": "v: {{ lookup('vars', 'widget_' ~ suffix) }}\n"}
    )
    assert len(found.problems) == 1
    assert "lookup('vars')" in found.problems[0]


def test_an_include_the_walk_cannot_resolve_is_refused(tmp_path):
    with pytest.raises(CensusError, match="cannot resolve"):
        _census(
            tmp_path,
            {"deployment.yaml.j2": "x: 1\n"},
            '- ansible.builtin.include_tasks: "{{ widget_step }}.yml"\n',
        )
