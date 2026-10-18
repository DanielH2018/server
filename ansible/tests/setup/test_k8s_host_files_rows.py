"""Every host row D5 has moved into `k8s_host_files` stays there, imported and unchanged.

D5's conversion PRs move each k8s role's host writes into `k8s_host_files`, one task file per
source role (D5's *Host-side rows*). A row that drops out of the role, or whose file is no
longer imported from `tasks/main.yml`, stops running on the next apply, and no deploy says so:
a missing directory surfaces only when a pod waits in ContainerCreating on its hostPath.

`EXPECTED_ROWS` names each moved row by task file and task name, with the module and the whole
argument map it must keep: path, state, owner, group and mode. A row that loops over `{{ item }}`
also pins its loop in `EXPECTED_LOOPS`. Each conversion PR adds its rows in the same commit as the move. The other
direction, a row coming back to its k8s role, is the host-write census's
(`ansible/tests/repo/_host_write_rows.py`): the move drops the role's `allow` entry, so a host
write there fails `k8s-role-tasks-write-no-host`.

Red-proof: a fixture role under `tmp_path` whose `main.yml` no longer imports the file, and
one whose task writes a different path, and one whose task drops its owner, must each be
flagged; a faithful copy must not.

Run: uv run pytest ansible/tests/setup/test_k8s_host_files_rows.py
"""

import shutil
from pathlib import Path

from _helpers import SETUP_ROLES, imported_task_files, leaf_tasks, load_tasks

ROLE = SETUP_ROLES / "k8s_host_files"

# {task file: {task name: (module, its args)}}, one file per source role. The args are the
# task's whole argument map, so an owner, group or mode change fails the row as well as a path:
# docs' directory exists to be owned by sys_user, which the docs-refresh build writes as.
EXPECTED_ROWS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    "artifacts.yml": {
        "Ensure the local artifacts directory exists": (
            "ansible.builtin.file",
            {
                "path": "{{ artifacts_host_dir }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0750",
            },
        ),
        "Ensure a peer directory exists for every synced host": (
            "ansible.builtin.file",
            {
                "path": "{{ artifacts_peer_dir }}/{{ item.name }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0750",
            },
        ),
        # 0755, not 0750: the agent user's prune reads the star list as `claude`.
        "Ensure the star list directory exists": (
            "ansible.builtin.file",
            {
                "path": "{{ artifacts_stars_dir }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0755",
            },
        ),
    },
    "configarr.yml": {
        "Remove the retired configarr sync heartbeat cron": (
            "ansible.builtin.cron",
            {
                "name": "configarr sync health",
                "user": "{{ sys_user }}",
                "cron_file": "configarr-health",
                "state": "absent",
            },
        ),
        "Remove the retired configarr health reader from the host": (
            "ansible.builtin.file",
            {"path": "{{ item }}", "state": "absent"},
        ),
    },
    "docs.yml": {
        "Ensure the built docs site directory exists": (
            "ansible.builtin.file",
            {
                "path": "{{ docs_host_site_dir }}",
                "state": "directory",
                "owner": "{{ sys_user }}",
                "group": "{{ sys_user }}",
                "mode": "0755",
            },
        ),
    },
}

# {(task file, task name): its loop}, for a row whose argument map writes `{{ item }}`: the
# map alone would let a loop drop a path unnoticed. configarr's arm removes three host paths.
EXPECTED_LOOPS: dict[tuple[str, str], list[str]] = {
    ("configarr.yml", "Remove the retired configarr health reader from the host"): [
        "/usr/local/bin/configarr-health.sh",
        "/opt/configarr-health",
        "/var/lib/homelab/setup-deployed-manifest.d/configarr",
    ],
}


def missing_rows(role_dir: Path = ROLE) -> list[str]:
    """`<file>: <task name>` for each expected row the role does not import as declared."""
    imported = {path.name: path for path in imported_task_files(role_dir)}
    missing = []
    for file, rows in sorted(EXPECTED_ROWS.items()):
        tasks = leaf_tasks(load_tasks(imported[file])) if file in imported else []
        by_name = {task.get("name"): task for task in tasks}
        for name, (module, expected) in sorted(rows.items()):
            task = by_name.get(name, {})
            loop = EXPECTED_LOOPS.get((file, name))
            if task.get(module) != expected or (loop and task.get("loop") != loop):
                missing.append(f"{file}: {name}")
    return missing


def test_every_moved_row_is_imported_unchanged():
    assert missing_rows() == []


def _fixture_role(tmp_path: Path) -> Path:
    """A copy of the live role's `tasks/` under `tmp_path`."""
    role_dir = tmp_path / "k8s_host_files"
    shutil.copytree(ROLE / "tasks", role_dir / "tasks")
    return role_dir


def test_a_faithful_copy_is_clean(tmp_path):
    assert missing_rows(_fixture_role(tmp_path)) == []


def test_a_task_file_main_no_longer_imports_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    (role_dir / "tasks" / "main.yml").write_text("---\n[]\n")
    assert missing_rows(role_dir) == [
        "artifacts.yml: Ensure a peer directory exists for every synced host",
        "artifacts.yml: Ensure the local artifacts directory exists",
        "artifacts.yml: Ensure the star list directory exists",
        "configarr.yml: Remove the retired configarr health reader from the host",
        "configarr.yml: Remove the retired configarr sync heartbeat cron",
        "docs.yml: Ensure the built docs site directory exists",
    ]


def test_a_row_writing_another_path_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    docs = role_dir / "tasks" / "docs.yml"
    docs.write_text(docs.read_text().replace("docs_host_site_dir", "docs_other_dir"))
    assert missing_rows(role_dir) == [
        "docs.yml: Ensure the built docs site directory exists"
    ]


def test_a_row_that_drops_its_owner_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    docs = role_dir / "tasks" / "docs.yml"
    docs.write_text(docs.read_text().replace('    owner: "{{ sys_user }}"\n', ""))
    assert missing_rows(role_dir) == [
        "docs.yml: Ensure the built docs site directory exists"
    ]


def test_an_artifacts_row_that_widens_the_star_list_mode_is_flagged(tmp_path):
    """The star list directory is the one 0755 row, so its mode is the field most likely to drift."""
    role_dir = _fixture_role(tmp_path)
    artifacts = role_dir / "tasks" / "artifacts.yml"
    artifacts.write_text(artifacts.read_text().replace('mode: "0755"', 'mode: "0777"'))
    assert missing_rows(role_dir) == [
        "artifacts.yml: Ensure the star list directory exists"
    ]


def test_a_configarr_arm_that_drops_a_path_from_its_loop_is_flagged(tmp_path):
    """The loop carries the paths, so a dropped one leaves the argument map unchanged."""
    role_dir = _fixture_role(tmp_path)
    configarr = role_dir / "tasks" / "configarr.yml"
    configarr.write_text(
        configarr.read_text().replace("    - /opt/configarr-health\n", "")
    )
    assert missing_rows(role_dir) == [
        "configarr.yml: Remove the retired configarr health reader from the host"
    ]


def test_a_configarr_cron_arm_that_turns_present_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    configarr = role_dir / "tasks" / "configarr.yml"
    configarr.write_text(
        configarr.read_text().replace("state: absent", "state: present", 1)
    )
    assert missing_rows(role_dir) == [
        "configarr.yml: Remove the retired configarr sync heartbeat cron"
    ]
