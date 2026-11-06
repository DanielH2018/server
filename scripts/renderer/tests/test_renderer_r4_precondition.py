"""R4's preconditions: Flux writes every k8s service, and its tasks hold no imperative row.

R4 deletes the k8s Ansible path and converts nothing, so it checks that D5 finished. A red
result stops R4, and the row goes back to D5 as a finding
(docs/target-architecture/render/r4-delete-k8s-ansible-path.md, *R4 deletes, D5 converts*).

Run: uv run pytest scripts/renderer/tests/test_renderer_r4_precondition.py
"""

from pathlib import Path

import pytest

from lib import yaml_fast
from lib.k8s_roles import k8s_entries
from lib.repo_paths import K8S_ROLES

MEMBER_FLOOR = {"pihole", "traefik", "flux"}
RUNBOOK_ALLOWLIST = (
    "volume-revert/tasks/main.yml",
    "volume-revert/tasks/claim.yml",
    "longhorn-api/tasks/main.yml",
    "longhorn-api/tasks/resolve.yml",
)
_RENDERED_INCLUDES = {"k8s/manifests", "k8s/image-builder"}
_INCLUDE_MODULES = {
    "include_role",
    "import_role",
    "ansible.builtin.include_role",
    "ansible.builtin.import_role",
}
_IMPORT_MODULES = {
    "import_tasks",
    "include_tasks",
    "ansible.builtin.import_tasks",
    "ansible.builtin.include_tasks",
}
_INCLUDE_KEYS = {"name", "vars", "when", "tags"}
_BLOCK_KEYS = ("block", "rescue", "always")


def owner_problems(entries: dict[str, dict]) -> list[str]:
    """Each k8s entry that Flux does not write."""
    return [
        f"{tag} carries owner {entry.get('owner')!r}, not flux"
        for tag, entry in sorted(entries.items())
        if entry.get("owner") != "flux"
    ]


def _module(task: dict, modules: set[str]) -> object | None:
    return next((task[key] for key in task if key in modules), None)


def _leaf_tasks(nodes: list | None):
    for task in nodes or []:
        if any(key in task for key in _BLOCK_KEYS):
            for key in _BLOCK_KEYS:
                yield from _leaf_tasks(task.get(key))
        else:
            yield task


def _permitted(task: dict, tasks_dir: Path) -> bool:
    included = _module(task, _INCLUDE_MODULES)
    if included is not None:
        return (
            isinstance(included, dict)
            and set(included) == {"name"}
            and included["name"] in _RENDERED_INCLUDES
            and set(task) - _INCLUDE_MODULES - {"name"} <= _INCLUDE_KEYS
        )
    imported = _module(task, _IMPORT_MODULES)
    if imported is not None:
        file = imported.get("file") if isinstance(imported, dict) else imported
        return (
            isinstance(file, str)
            and "{" not in file
            and (tasks_dir / file).resolve().is_relative_to(tasks_dir.resolve())
            and (tasks_dir / file).is_file()
        )
    return False


def grammar_problems(entries: dict[str, dict], roles: Path) -> list[str]:
    """Each task of a domain role that the grammar refuses, as `role/file: task name`.

    `flux` is the one named exemption, and only while it is flux-owned: no path runs a
    flux-owned role's tasks, and R4 deletes its D1 install code.
    """
    problems = []
    for tag, entry in sorted(entries.items()):
        if tag == "flux" and entry.get("owner") == "flux":
            continue
        tasks_dir = roles / tag / "tasks"
        for path in sorted(p for p in tasks_dir.rglob("*") if p.is_file()):
            for task in _leaf_tasks(yaml_fast.safe_load(path.read_text())):
                if not _permitted(task, tasks_dir):
                    problems.append(f"{tag}/{path.name}: {task.get('name')}")
    return problems


def floor_problems(entries: dict[str, dict]) -> list[str]:
    """The named members a real domain must hold, so a shrunken read cannot pass."""
    return [
        f"{tag} is not in the domain" for tag in sorted(MEMBER_FLOOR - set(entries))
    ]


def missing_runbook_files(roles: Path, allowlist: tuple[str, ...]) -> list[str]:
    """Each allowlisted runbook task file absent from `roles`."""
    return [rel for rel in allowlist if not (roles / rel).is_file()]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


KUBECTL = """\
- name: Patch the workload
  ansible.builtin.command:
    cmd: k3s kubectl patch deploy/demo
"""
RENDER_INCLUDE = """\
- name: Deploy demo
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_rollout: demo
"""
HOST_LIB_IMPORT = """\
- name: Install the host library
  ansible.builtin.import_tasks: "{{ role_path }}/../../setup/common/tasks/install_host_lib.yml"
"""
D1_TASKS = """\
- name: Wait for the Flux controllers to roll out
  ansible.builtin.command:
    cmd: k3s kubectl -n flux-system rollout status deploy/source-controller
"""
OWNED = {"owner": "flux"}


def test_every_k8s_service_is_flux_owned_on_the_tree():
    entries = k8s_entries()
    assert not floor_problems(entries)
    assert owner_problems(entries) == []


def test_a_service_without_flux_as_its_writer_fails_and_a_flux_owned_one_passes():
    assert owner_problems({"demo": {}}) == ["demo carries owner None, not flux"]
    assert owner_problems({"demo": {"owner": "released"}}) != []
    assert owner_problems({"demo": OWNED}) == []


def test_every_domain_role_meets_the_task_grammar_on_the_tree():
    assert grammar_problems(k8s_entries(), K8S_ROLES) == []


def test_a_domain_without_traefik_fails_the_member_floor():
    assert floor_problems({"pihole": OWNED, "flux": OWNED}) == [
        "traefik is not in the domain"
    ]


def test_a_kubectl_command_task_fails(tmp_path):
    _write(tmp_path, "demo/tasks/main.yml", RENDER_INCLUDE + KUBECTL)
    assert grammar_problems({"demo": OWNED}, tmp_path) == [
        "demo/main.yml: Patch the workload"
    ]


def test_an_imported_sibling_holding_a_kubectl_command_task_fails(tmp_path):
    _write(
        tmp_path, "demo/tasks/main.yml", "- ansible.builtin.import_tasks: extra.yml\n"
    )
    _write(tmp_path, "demo/tasks/extra.yml", KUBECTL)
    assert grammar_problems({"demo": OWNED}, tmp_path) == [
        "demo/extra.yml: Patch the workload"
    ]


def test_an_imported_sibling_of_any_suffix_is_read(tmp_path):
    _write(
        tmp_path, "demo/tasks/main.yml", "- ansible.builtin.import_tasks: extra.yaml\n"
    )
    _write(tmp_path, "demo/tasks/extra.yaml", KUBECTL)
    assert grammar_problems({"demo": OWNED}, tmp_path) == [
        "demo/extra.yaml: Patch the workload"
    ]


def test_a_kubectl_command_task_inside_a_block_fails(tmp_path):
    _write(
        tmp_path,
        "demo/tasks/main.yml",
        "- name: Wrapper\n  block:\n"
        + "".join(f"    {line}\n" for line in KUBECTL.splitlines()),
    )
    assert grammar_problems({"demo": OWNED}, tmp_path) == [
        "demo/main.yml: Patch the workload"
    ]


def test_a_templated_import_outside_the_role_fails(tmp_path):
    _write(tmp_path, "demo/tasks/main.yml", HOST_LIB_IMPORT)
    assert grammar_problems({"demo": OWNED}, tmp_path) == [
        "demo/main.yml: Install the host library"
    ]


def test_the_flux_exemption_names_the_other_role_alone(tmp_path):
    _write(tmp_path, "flux/tasks/main.yml", D1_TASKS)
    _write(tmp_path, "demo/tasks/main.yml", KUBECTL)
    assert grammar_problems({"flux": OWNED, "demo": OWNED}, tmp_path) == [
        "demo/main.yml: Patch the workload"
    ]


def test_a_flux_entry_without_the_ownership_flag_fails_on_flux(tmp_path):
    _write(tmp_path, "flux/tasks/main.yml", D1_TASKS)
    assert grammar_problems({"flux": {}}, tmp_path) == [
        "flux/main.yml: Wait for the Flux controllers to roll out"
    ]


def test_a_set_fact_task_fails(tmp_path):
    _write(
        tmp_path,
        "demo/tasks/main.yml",
        "- name: Set the fact\n  ansible.builtin.set_fact:\n    demo_fact: true\n",
    )
    assert grammar_problems({"demo": OWNED}, tmp_path) == [
        "demo/main.yml: Set the fact"
    ]


@pytest.mark.parametrize(
    "extra",
    ["  when: demo_on | bool\n", "  tags: [deploy]\n"],
)
def test_a_render_include_may_carry_a_condition_or_tags(tmp_path, extra):
    _write(tmp_path, "demo/tasks/main.yml", RENDER_INCLUDE + extra)
    assert grammar_problems({"demo": OWNED}, tmp_path) == []


def test_an_include_of_another_role_or_entry_point_fails(tmp_path):
    other = RENDER_INCLUDE.replace("k8s/manifests", "k8s/longhorn-api")
    drain = RENDER_INCLUDE.replace(
        "    name: k8s/manifests\n", "    name: k8s/manifests\n    tasks_from: drain\n"
    )
    _write(tmp_path, "demo/tasks/main.yml", other + drain)
    assert len(grammar_problems({"demo": OWNED}, tmp_path)) == 2


def test_every_allowlisted_runbook_file_exists_on_the_tree():
    assert missing_runbook_files(K8S_ROLES, RUNBOOK_ALLOWLIST) == []


def test_a_tree_missing_an_allowlisted_runbook_file_fails(tmp_path):
    _write(tmp_path, RUNBOOK_ALLOWLIST[0], "---\n")
    assert missing_runbook_files(tmp_path, RUNBOOK_ALLOWLIST[:2]) == [
        RUNBOOK_ALLOWLIST[1]
    ]
