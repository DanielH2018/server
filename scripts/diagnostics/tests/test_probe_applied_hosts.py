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


def test_the_k3s_server_half_walks_on_daniel_box_and_the_agent_half_on_daniel_server():
    files = applied_hosts.task_files("k3s", K3S)
    assert "main" in files["daniel-box"]
    assert "main" not in files["daniel-server"]
    assert {"agent", "static-address"} <= set(files["daniel-server"])
    assert "main" not in files.get("daniel-pi", [])


def test_the_k3s_agent_walk_holds_none_of_the_server_crons():
    """daniel-server read the server play's crons as `missing` after a correct apply."""
    files = applied_hosts.task_files("k3s", K3S)["daniel-server"]
    ctx = applied.context(K3S, "daniel-server")
    wants, _ = applied_tasks.expected(K3S, "daniel-server", ctx, tuple(files))
    paths = {w.path for w in wants}
    assert "/etc/rancher/k3s/registries.yaml" in paths
    assert not {p for p in paths if p.startswith("/opt/longhorn-backup-health")}
    assert "/etc/rancher/k3s/kuma-push.env" not in paths


@pytest.mark.parametrize(
    "path",
    [
        "/etc/rancher/k3s/kuma-push.env",
        "/etc/healthchecks/ping.env",
        "/etc/rancher/k3s/live-drift-check.env",
    ],
)
def test_a_secret_read_through_default_is_not_compared_by_content(path):
    """`{{ token | default("") }}` rendered empty and was compared against the live token."""
    wants, _ = applied_tasks.expected(
        K3S, "daniel-box", applied.context(K3S, "daniel-box")
    )
    (want,) = {w for w in wants if w.path == path}
    assert want.sha256 is None
    assert "STUB" in want.why_no_content


def test_a_host_whose_files_write_nothing_does_not_make_the_run_partial(tmp_path):
    role = tmp_path / "x" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
        '- ansible.builtin.copy: {content: "x\\n", dest: /etc/x}\n'
    )
    (role / "verify.yml").write_text("- ansible.builtin.command: /bin/true\n")
    row = {"status": "ok", "mode": "0644", "owner": "root", "group": "root"}

    def read(plan, host):
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
