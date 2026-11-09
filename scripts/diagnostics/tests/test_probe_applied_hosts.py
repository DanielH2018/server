"""Tests for which task files `probe.py applied` walks on each host (`probe_lib/applied_hosts.py`).

The synthetic tests write a playbook to `tmp_path`; the real ones read `k3s-bringup.yml` and the
k3s role, whose server and agent halves are separate plays (#6475).
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from diagnostics.probe_lib import applied, applied_hosts, applied_tasks
from lib.exit_codes import PROBE_OK

K3S = applied.SETUP / "k3s"
PLAYBOOK = """\
- hosts: all
  pre_tasks:
    - name: Refuse any other host
      ansible.builtin.assert:
        that: {that}
  roles:
    - {{ role: x }}
- hosts: "{{{{ join | default([]) }}}}"
  pre_tasks:
    - ansible.builtin.assert: {{that: "inventory_hostname == 'daniel-server'"}}
  tasks:
    - ansible.builtin.include_role: {{name: x, tasks_from: half}}
"""


def _files(tmp_path, that):
    (tmp_path / "initial_setup.yml").write_text(PLAYBOOK.format(that=that))
    return applied_hosts.task_files("x", tmp_path / "x", ansible=tmp_path)


def test_a_play_assert_keeps_the_role_off_the_hosts_it_refuses(tmp_path):
    files = _files(tmp_path, "\"inventory_hostname == 'daniel-box'\"")
    assert files == {"daniel-server": ["half"], "daniel-box": ["main"]}


def test_an_assert_the_probe_cannot_read_admits_every_host(tmp_path):
    files = _files(tmp_path, "some_fact.rc == 0")
    assert files["daniel-pi"] == ["main"]
    assert files["daniel-server"] == ["main", "half"]


@pytest.mark.parametrize(
    "hosts, admitted",
    [
        (
            "\"{{ go | default([]) is truthy | ternary('daniel-box', []) }}\"",
            ["daniel-box"],
        ),
        ("daniel-box,daniel-server", ["daniel-server", "daniel-box"]),
        ('"{{ go | default([]) }}"', ["daniel-server", "daniel-pi", "daniel-box"]),
    ],
)
def test_a_play_hosts_bounds_the_role_only_when_it_names_a_host(
    tmp_path, hosts, admitted
):
    (tmp_path / "initial_setup.yml").write_text(
        f"- hosts: {hosts}\n  tasks:\n"
        "    - ansible.builtin.include_role: {name: x, tasks_from: verify}\n"
    )
    assert (
        list(applied_hosts.task_files("x", tmp_path / "x", ansible=tmp_path))
        == admitted
    )


def test_the_emptied_k3s_role_walks_only_the_files_its_join_plays_name():
    files = applied_hosts.task_files("k3s", K3S)
    assert files["daniel-box"] == ["agent_verify"]
    assert files["daniel-server"] == ["static-address", "agent"]
    assert "daniel-pi" not in files


def test_the_k3s_agent_walk_holds_none_of_the_server_crons():
    """daniel-server read the server play's crons as `missing` after a correct apply."""
    files = applied_hosts.task_files("k3s", K3S)["daniel-server"]
    ctx = applied.context(K3S, "daniel-server")
    wants, _ = applied_tasks.expected(K3S, "daniel-server", ctx, tuple(files))
    paths = {w.path for w in wants}
    # daniel-server's k3s half, the pin play's file included, is its host package's since H7.34.
    assert paths == set()


def test_a_host_whose_files_write_nothing_is_not_read_and_not_a_gap(tmp_path):
    """The Pi is down or rate-limited often; a host with no path must not turn that into 75."""
    role = tmp_path / "x" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
        '- ansible.builtin.copy: {content: "x\\n", dest: /etc/x}\n'
    )
    (role / "verify.yml").write_text("- ansible.builtin.command: /bin/true\n")
    row = {"status": "ok", "mode": "0644", "owner": "root", "group": "root"}

    def read(plan, host):
        if host == "daniel-pi":
            raise applied.core.Unreadable("ssh: connect to host daniel-pi: refused")
        return {"user": "ubuntu", "stat": {p: row for p in plan["stat"]}}

    ns = SimpleNamespace(role="x", host=None, dry_run=False)
    code = applied.run_applied(
        ns,
        read=read,
        setup=tmp_path,
        render_ctx=lambda *_: {},
        files_of=lambda *_: {"daniel-box": ["main"], "daniel-pi": ["verify"]},
    )
    assert code == PROBE_OK
