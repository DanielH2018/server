"""A `delegate_to:` task joins the two-writer census on its target host, not on the play's host.

The setup plane's playbook entries are gated to daniel-box, so a task that writes daniel-pi
carries `delegate_to: daniel-pi`, on itself or on its enclosing block. Read as daniel-box's, the
census never compared such a task with the Pi package, and a package could take over a path while
the task still wrote it (H5.14, docs/target-architecture/host/h5-pi-compose.md).
`test_host_package_two_writers.ansible_writes` attributes the write to the delegate host.

- **Named member.** pi-peer-backup's block, the one live `delegate_to: daniel-pi` task in the
  setup plane, must land on daniel-pi and nowhere else.
- **Red proof.** A fixture role whose block has the shape crowdsec's probe timer had, delegated to
  daniel-pi, collides with a Pi package retiring that unit. The same block without the delegation
  is attributed to daniel-box, and flags nothing.

Run: uv run pytest ansible/tests/setup/test_delegated_writes_attribute_to_target.py
"""

from pathlib import Path

import pytest
from _role_census import task_files_by_role
from deploy_tools.setup_lib.setup_gates import HOSTS
from test_host_package_two_writers import (
    SETUP,
    ansible_writes,
    collisions,
    package_writes,
)

WRAPPER = ("path", "/usr/local/bin/pi-peer-backup-shell")
TIMER = "/etc/systemd/system/crowdsec-edge-probe.timer"

BLOCK = """\
- name: Install the probe timer
  {delegation}
  become: true
  block:
    - name: Install the probe timer unit
      ansible.builtin.template:
        src: probe.timer.j2
        dest: /etc/systemd/system/crowdsec-edge-probe.timer
"""


def test_the_pi_peer_backup_block_is_written_on_the_pi_only():
    ansible = ansible_writes(task_files_by_role(SETUP), HOSTS)
    assert WRAPPER in ansible["daniel-pi"]
    assert WRAPPER not in ansible["daniel-box"]


def _collisions(tmp_path: Path, delegation: str) -> tuple[list[str], dict]:
    roles = tmp_path / "roles"
    (roles / "demo" / "tasks").mkdir(parents=True)
    (task_file := roles / "demo" / "tasks" / "main.yml").write_text(
        BLOCK.format(delegation=delegation)
    )
    pi = tmp_path / "hosts" / "daniel-pi"
    (tmp_path / "hosts" / "packages").mkdir(parents=True)
    pi.mkdir()
    (pi / "package.yml").write_text("legacy:\n  - {unit: crowdsec-edge-probe.timer}\n")
    ansible = ansible_writes(
        [(roles / "demo", task_file)], HOSTS, lambda _: frozenset({"daniel-box"})
    )
    return collisions(ansible, package_writes(tmp_path / "hosts", HOSTS)), ansible


def test_a_block_delegated_to_the_pi_collides_with_the_pi_packages_legacy_unit(
    tmp_path,
):
    found, ansible = _collisions(tmp_path, "delegate_to: daniel-pi")
    assert ("path", TIMER) not in ansible["daniel-box"]
    assert len(found) == 1
    assert found[0].startswith(f"daniel-pi: path {TIMER} is written by ")
    assert found[0].endswith("and shipped by ['homelab-daniel-pi (legacy)']")


@pytest.mark.parametrize("delegation", ["tags: [demo]", "delegate_to: localhost"])
def test_a_block_that_stays_on_the_play_host_collides_with_nothing(
    tmp_path, delegation
):
    found, ansible = _collisions(tmp_path, delegation)
    assert ("path", TIMER) in ansible["daniel-box"]
    assert found == []
