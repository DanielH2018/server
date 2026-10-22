"""Every host row D5 has moved into `k8s_host_files` stays there, imported and unchanged.

D5's conversion PRs move each k8s role's host writes into `k8s_host_files`, one task file per
source role (D5's *Host-side rows*). A row that drops out of the role, or whose file is no
longer imported from `tasks/main.yml`, stops running on the next apply, and no deploy says so:
a missing directory surfaces only when a pod waits in ContainerCreating on its hostPath.

`EXPECTED_ROWS` in `_k8s_host_files_expected.py` names each moved row by task file and task
name, with the module and the whole argument map it must keep: path, state, owner, group and
mode. A looped row's path reads `item`, so `EXPECTED_LOOPS` pins its loop too. Each conversion
PR adds its rows in the same commit as the move. The other
direction, a row coming back to its k8s role, is the host-write census's
(`ansible/tests/repo/_host_write_rows.py`): the move drops the role's `allow` entry, so a host
write there fails `k8s-role-tasks-write-no-host`.

pi-peer-backup's two rows and crowdsec's edge probe rows write daniel-pi, and the play runs on
daniel-box, so `rows_on_the_wrong_host` also holds each to `delegate_to: daniel-pi`, and
crowdsec's absent arms, which clean daniel-box, to no delegation.

Red-proof: a fixture role under `tmp_path` whose `main.yml` no longer imports the file, one
whose task writes a different path, one whose task drops its owner, one whose loop drops a
directory, one whose absent arm turns back into an install, one whose Pi block drops its
delegation, and one whose crowdsec arm gains it, must each be flagged; a faithful copy must not.

Run: uv run pytest ansible/tests/setup/test_k8s_host_files_rows.py
"""

import shutil
from pathlib import Path

from _helpers import SETUP_ROLES, imported_task_files, leaf_tasks, load_tasks
from _k8s_host_files_expected import CROWDSEC_ARMS, EXPECTED_LOOPS, EXPECTED_ROWS

ROLE = SETUP_ROLES / "k8s_host_files"


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
        "configarr.yml: Remove the retired configarr health reader from the host",
        "configarr.yml: Remove the retired configarr sync heartbeat cron",
        "crowdsec.yml: Enable the edge probe timer on daniel-pi",
        "crowdsec.yml: Install the edge probe script on daniel-pi",
        "crowdsec.yml: Install the edge probe service on daniel-pi",
        "crowdsec.yml: Install the edge probe timer on daniel-pi",
        "crowdsec.yml: Remove the retired AppSec verify cron",
        "crowdsec.yml: Remove the retired bouncer-prune cron",
        "crowdsec.yml: Remove the retired crowdsec cron scripts from the host",
        "crowdsec.yml: Remove the retired home allowlist cron",
        "crowdsec.yml: Remove the retired remote-allowlist cron",
        "docs.yml: Ensure the built docs site directory exists",
        "janitorr.yml: Remove the retired janitorr error heartbeat cron",
        "janitorr.yml: Remove the retired janitorr health reader from the host",
        "media-volume.yml: Create the media volume directories on the node",
        "observability.yml: Remove the retired dashboard staging trees",
        "pi-peer-backup.yml: Authorize the pull key on daniel-pi",
        "pi-peer-backup.yml: Install the forced-command wrapper on daniel-pi",
        "qbittorrent.yml: Remove the retired qbittorrent prefs drift check cron",
        "qbittorrent.yml: Remove the retired qbittorrent prefs-check script from the host",
        "registry.yml: Create the registry garbage-collection state directory",
        "registry.yml: Create the registry job manifest directory",
        "registry.yml: Deploy the registry garbage-collection script",
        "registry.yml: Remove the retired push store's garbage-collection job manifest",
        "registry.yml: Render the ghcr-mirror garbage-collection job",
        "registry.yml: Schedule the registry garbage collection",
        "traefik.yml: Remove the Cloudflare IP drift cron",
        "traefik.yml: Remove the Cloudflare IP drift script",
        "uptime-kuma.yml: Remove the k8s/volume-claim staging directory autokuma-data left behind",
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


# {task file: the rows in it that write on daniel-pi}. Every other row there runs on the play's
# host, daniel-box.
PI_ROWS = {
    "crowdsec.yml": set(EXPECTED_ROWS["crowdsec.yml"]) - set(CROWDSEC_ARMS),
    "pi-peer-backup.yml": set(EXPECTED_ROWS["pi-peer-backup.yml"]),
}


def rows_on_the_wrong_host(role_dir: Path = ROLE) -> list[str]:
    """Each row in a file that writes daniel-pi which runs on the other host.

    The playbook entry is gated to daniel-box, so a daniel-pi row whose task and enclosing
    block both lose `delegate_to: daniel-pi` installs its file on the wrong host. A daniel-box
    row moved inside the Pi block removes its cron and files from the Pi instead.
    """
    found = []
    for name, pi_rows in PI_ROWS.items():
        for wrapper in load_tasks(role_dir / "tasks" / name):
            for task in wrapper.get("block") or [wrapper]:
                host = task.get("delegate_to", wrapper.get("delegate_to"))
                if (host == "daniel-pi") != (task.get("name") in pi_rows):
                    found.append(f"{name}: {task.get('name')}")
    return found


def test_the_daniel_pi_rows_run_on_daniel_pi():
    assert rows_on_the_wrong_host() == []


def test_a_crowdsec_probe_block_that_drops_its_delegation_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    probe = role_dir / "tasks" / "crowdsec.yml"
    line = "  delegate_to: daniel-pi\n"
    assert line in probe.read_text()
    probe.write_text(probe.read_text().replace(line, ""))
    assert rows_on_the_wrong_host(role_dir) == [
        f"crowdsec.yml: {name}"
        for name in EXPECTED_ROWS["crowdsec.yml"]
        if name not in CROWDSEC_ARMS
    ]


def test_a_crowdsec_arm_delegated_to_the_pi_is_flagged(tmp_path):
    """The arms remove daniel-box's crons, so one delegated to the Pi removes nothing."""
    role_dir = _fixture_role(tmp_path)
    tasks = role_dir / "tasks" / "crowdsec.yml"
    line = "- name: Remove the retired home allowlist cron\n"
    assert line in tasks.read_text()
    tasks.write_text(
        tasks.read_text().replace(line, line + "  delegate_to: daniel-pi\n")
    )
    assert rows_on_the_wrong_host(role_dir) == [
        "crowdsec.yml: Remove the retired home allowlist cron"
    ]


def test_a_pi_peer_backup_block_that_drops_its_delegation_is_flagged(tmp_path):
    role_dir = _fixture_role(tmp_path)
    pi = role_dir / "tasks" / "pi-peer-backup.yml"
    line = "  delegate_to: daniel-pi\n"
    assert line in pi.read_text()
    pi.write_text(pi.read_text().replace(line, ""))
    assert rows_on_the_wrong_host(role_dir) == [
        "pi-peer-backup.yml: Install the forced-command wrapper on daniel-pi",
        "pi-peer-backup.yml: Authorize the pull key on daniel-pi",
    ]


def test_a_traefik_arm_turned_back_into_an_install_is_flagged(tmp_path):
    """An arm whose `state` reads `present` reinstalls the cron it exists to remove."""
    role_dir = _fixture_role(tmp_path)
    traefik = role_dir / "tasks" / "traefik.yml"
    line = "    cron_file: cloudflare-ip-drift\n    state: absent\n"
    assert line in traefik.read_text()
    traefik.write_text(
        traefik.read_text().replace(line, line.replace("absent", "present"))
    )
    assert missing_rows(role_dir) == [
        "traefik.yml: Remove the Cloudflare IP drift cron"
    ]
