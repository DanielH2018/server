"""Every host row D5 has moved into `k8s_host_files` stays there, imported and unchanged.

D5's conversion PRs move each k8s role's host writes into `k8s_host_files`, one task file per
source role (D5's *Host-side rows*). A row that drops out of the role, or whose file is no
longer imported from `tasks/main.yml`, stops running on the next apply, and no deploy says so:
a missing directory surfaces only when a pod waits in ContainerCreating on its hostPath.

`EXPECTED_ROWS` names each moved row by task file and task name, with the module, path and
state it must keep. Each conversion PR adds its rows in the same commit as the move. The other
direction, a row coming back to its k8s role, is the host-write census's
(`ansible/tests/repo/_host_write_rows.py`): the move drops the role's `allow` entry, so a host
write there fails `k8s-role-tasks-write-no-host`.

Red-proof: a fixture role under `tmp_path` whose `main.yml` no longer imports the file, and
one whose task writes a different path, must each be flagged; a faithful copy must not.

Run: uv run pytest ansible/tests/setup/test_k8s_host_files_rows.py
"""

import shutil
from pathlib import Path

from _helpers import SETUP_ROLES, imported_task_files, leaf_tasks, load_tasks

ROLE = SETUP_ROLES / "k8s_host_files"

# {task file: {task name: (module, path, state)}}, one file per source role.
EXPECTED_ROWS: dict[str, dict[str, tuple[str, str, str]]] = {
    "docs.yml": {
        "Ensure the built docs site directory exists": (
            "ansible.builtin.file",
            "{{ docs_host_site_dir }}",
            "directory",
        ),
    },
}


def missing_rows(role_dir: Path = ROLE) -> list[str]:
    """`<file>: <task name>` for each expected row the role does not import as declared."""
    imported = {path.name: path for path in imported_task_files(role_dir)}
    missing = []
    for file, rows in sorted(EXPECTED_ROWS.items()):
        tasks = leaf_tasks(load_tasks(imported[file])) if file in imported else []
        by_name = {task.get("name"): task for task in tasks}
        for name, (module, path, state) in sorted(rows.items()):
            args = by_name.get(name, {}).get(module)
            if not (
                isinstance(args, dict)
                and args.get("path") == path
                and args.get("state") == state
            ):
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
        "docs.yml: Ensure the built docs site directory exists"
    ]


def test_a_row_writing_another_path_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    docs = role_dir / "tasks" / "docs.yml"
    docs.write_text(docs.read_text().replace("docs_host_site_dir", "docs_other_dir"))
    assert missing_rows(role_dir) == [
        "docs.yml: Ensure the built docs site directory exists"
    ]
