"""Every host row D5 has moved into `k8s_host_files` stays there, imported and unchanged.

D5's conversion PRs move each k8s role's host writes into `k8s_host_files`, one task file per
source role (D5's *Host-side rows*). A row that drops out of the role, or whose file is no
longer imported from `tasks/main.yml`, stops running on the next apply, and no deploy says so:
a missing directory surfaces only when a pod waits in ContainerCreating on its hostPath.

`EXPECTED_ROWS` names each moved row by task file and task name, with the module and the whole
argument map it must keep: path, state, owner, group and mode. A looped row's path reads
`item`, so `EXPECTED_LOOPS` pins its loop too. Each conversion PR adds its rows in the same
commit as the move. The other
direction, a row coming back to its k8s role, is the host-write census's
(`ansible/tests/repo/_host_write_rows.py`): the move drops the role's `allow` entry, so a host
write there fails `k8s-role-tasks-write-no-host`.

pi-peer-backup's two rows write daniel-pi, and the play runs on daniel-box, so
`undelegated_pi_rows` also holds each to `delegate_to: daniel-pi`.

Red-proof: a fixture role under `tmp_path` whose `main.yml` no longer imports the file, one
whose task writes a different path, one whose task drops its owner, one whose loop drops a
directory, and one whose pi-peer-backup block drops its delegation, must each be flagged; a
faithful copy must not.

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
    # The wrapper first, then the key pinned to it: the reverse order leaves a forced command
    # that does not exist. Both run on daniel-pi through the block's `delegate_to`.
    "pi-peer-backup.yml": {
        "Install the forced-command wrapper on daniel-pi": (
            "ansible.builtin.copy",
            {
                "src": "pi-peer-backup-shell.sh",
                "dest": "/usr/local/bin/pi-peer-backup-shell",
                "owner": "root",
                "group": "root",
                "mode": "0755",
            },
        ),
        "Authorize the pull key on daniel-pi": (
            "ansible.posix.authorized_key",
            {
                "user": "{{ hostvars['daniel-pi'].ansible_user | default('ubuntu') }}",
                "key": "{{ lookup('file', 'pi-peer-backup.pub') }}",
                "key_options": (
                    'restrict,command="/usr/local/bin/pi-peer-backup-shell '
                    '{{ pi_peer_backup_src_dir }}"'
                ),
                "state": "present",
            },
        ),
    },
    # No mode, on purpose: the library keeps the directory modes it came with.
    "media-volume.yml": {
        "Create the media volume directories on the node": (
            "ansible.builtin.file",
            {
                "path": "{{ item }}",
                "state": "directory",
                "owner": "{{ puid }}",
                "group": "{{ pgid }}",
            },
        ),
    },
}

# {(task file, task name): its `loop`} for every looped row. A row absent here must not loop.
# media-volume's hardlink probe links between media/ and torrents/, so both must exist.
EXPECTED_LOOPS: dict[tuple[str, str], object] = {
    (
        "artifacts.yml",
        "Ensure a peer directory exists for every synced host",
    ): "{{ artifacts_peer_sources }}",
    ("media-volume.yml", "Create the media volume directories on the node"): [
        "{{ media_volume_host_path }}",
        "{{ media_volume_host_path }}/media",
        "{{ media_volume_host_path }}/torrents",
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
            if task.get(module) != expected or task.get("loop") != EXPECTED_LOOPS.get(
                (file, name)
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
        "artifacts.yml: Ensure a peer directory exists for every synced host",
        "artifacts.yml: Ensure the local artifacts directory exists",
        "artifacts.yml: Ensure the star list directory exists",
        "docs.yml: Ensure the built docs site directory exists",
        "media-volume.yml: Create the media volume directories on the node",
        "pi-peer-backup.yml: Authorize the pull key on daniel-pi",
        "pi-peer-backup.yml: Install the forced-command wrapper on daniel-pi",
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


def test_a_media_volume_row_that_drops_a_directory_from_its_loop_is_flagged(tmp_path):
    """The path reads `item`, so only the loop says torrents/ is still created."""
    role_dir = _fixture_role(tmp_path)
    media = role_dir / "tasks" / "media-volume.yml"
    line = '    - "{{ media_volume_host_path }}/torrents"\n'
    assert line in media.read_text()
    media.write_text(media.read_text().replace(line, ""))
    assert missing_rows(role_dir) == [
        "media-volume.yml: Create the media volume directories on the node"
    ]


def undelegated_pi_rows(role_dir: Path = ROLE) -> list[str]:
    """Each pi-peer-backup row that would run on daniel-box, the play's host, not daniel-pi.

    The playbook entry is gated to daniel-box, so a row whose task and enclosing block both
    lose `delegate_to: daniel-pi` installs the wrapper and the key on the wrong host.
    """
    path = role_dir / "tasks" / "pi-peer-backup.yml"
    found = []
    for wrapper in load_tasks(path):
        for task in wrapper.get("block") or [wrapper]:
            host = task.get("delegate_to", wrapper.get("delegate_to"))
            if host != "daniel-pi":
                found.append(f"pi-peer-backup.yml: {task.get('name')}")
    return found


def test_the_pi_peer_backup_rows_run_on_daniel_pi():
    assert undelegated_pi_rows() == []


def test_a_pi_peer_backup_block_that_drops_its_delegation_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    pi = role_dir / "tasks" / "pi-peer-backup.yml"
    line = "  delegate_to: daniel-pi\n"
    assert line in pi.read_text()
    pi.write_text(pi.read_text().replace(line, ""))
    assert undelegated_pi_rows(role_dir) == [
        "pi-peer-backup.yml: Install the forced-command wrapper on daniel-pi",
        "pi-peer-backup.yml: Authorize the pull key on daniel-pi",
    ]
