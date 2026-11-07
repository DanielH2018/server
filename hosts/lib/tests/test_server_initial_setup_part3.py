"""daniel-server's host baseline and Loki witness move into its package (H7.59).

`test_pi_initial_setup_part3.py` holds the `host-hardening` component's content. These tests hold
what is specific to this host: the i915 option, the role's guards, the witness's uv path and
textfile directory on a rebuilt host, and the retirement of what the role left behind.

Run: uv run pytest hosts/lib/tests/test_server_initial_setup_part3.py
"""

import json

import pytest
from lib import yaml_fast
from lib.repo_paths import ANSIBLE

from deploy_tools.setup_lib.setup_gates import packaged_guard_hosts
from renderer.renderer_lib import host as host_mode

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
HOST = "daniel-server"
I915 = "/etc/modprobe.d/i915.conf"
WITNESS = "/usr/lib/homelab/loki-read-route-health"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-loki-route-witness.conf"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host(HOST)


def _legacy(package: host_mode.Package) -> list[dict]:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])["legacy"]


def test_a_rebuilt_daniel_servers_witness_names_a_uv_its_package_ships(package):
    script = package.files[WITNESS][0].decode()
    assert "/usr/bin/uv run" in script
    assert "/usr/local/bin/uv" not in script
    assert "/usr/bin/uv" in package.files


def test_the_old_uv_copy_retires_here_and_stays_on_daniel_box(package):
    assert {"path": "/usr/local/bin/uv"} in _legacy(package)
    box = host_mode.render_host("daniel-box")
    assert {"path": "/usr/local/bin/uv"} not in _legacy(box)


def test_the_role_no_longer_renders_a_witness():
    tasks = (ROLE / "tasks" / "crons.yml").read_text()
    assert "loki-read-route-health" not in tasks


def test_the_witness_user_can_write_a_rebuilt_hosts_textfile_directory(package):
    # The kubelet creates this directory root 0755, and the witness runs as the sys user.
    rows = [
        line.split()
        for line in package.files[TMPFILES][0].decode().splitlines()
        if line and not line.startswith("#")
    ]
    assert rows == [
        ["d", "/var/lib/node-exporter-textfile", "2775", "root", "ubuntu", "-"]
    ]


def test_the_ansible_era_witness_retires_after_the_reader_it_holds(package):
    legacy = _legacy(package)
    assert {"unit": "kuma-check-loki-read-route.timer"} in legacy
    paths = [entry.get("path") for entry in legacy]
    # Reconcile removes a directory only once it is empty.
    assert paths.index("/opt/loki-route-health/loki_route_health.py") < paths.index(
        "/opt/loki-route-health"
    )


def test_only_daniel_server_ships_the_i915_option(package):
    data, mode = package.files[I915]
    assert data == b"options i915 enable_guc=2\n"
    assert mode == 0o644
    assert I915 not in host_mode.render_host("daniel-pi").files


@pytest.mark.parametrize(
    "file, name, subject",
    [
        ("host-basics.yml", "Update and upgrade system packages", "host_basics"),
        ("host-basics.yml", "Install required packages", "host_basics"),
        (
            "host-basics.yml",
            "Install apt-show-versions (roomy hosts only)",
            "host_basics",
        ),
        (
            "system-tuning.yml",
            "Keep the info-level forwarding out of /var/log/syslog",
            "rsyslog_filter",
        ),
    ],
)
def test_the_role_skips_each_subject_here_and_keeps_daniel_box(file, name, subject):
    tasks = yaml_fast.safe_load((ROLE / "tasks" / file).read_text())
    when = next(task for task in tasks if task["name"] == name)["when"]
    assert f"initial_setup_packaged_hosts['{subject}']" in str(when)
    guards = when if isinstance(when, list) else [when]
    assert packaged_guard_hosts(guards, (HOST, "daniel-box"), ROLE) == {HOST}


@pytest.mark.parametrize(
    "subject", ["access", "accounting", "audit_and_kernel", "integrity"]
)
def test_each_whole_file_subject_skips_daniel_server_at_its_import(subject):
    imports = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    entry = next(task for task in imports if subject in str(task.get("when")))
    hosts = ("daniel-pi", "daniel-box", HOST)
    assert packaged_guard_hosts([entry["when"]], hosts, ROLE) == {"daniel-pi", HOST}


def test_the_role_no_longer_reverts_the_governor_or_removes_the_snaps():
    tasks = (ROLE / "tasks" / "system-tuning.yml").read_text()
    assert "scaling_governor" not in tasks
    assert "community.general.snap" not in tasks
