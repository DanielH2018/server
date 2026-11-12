"""daniel-box's host baseline moves into its package, and the role's tasks for it go (H7.60).

`test_pi_initial_setup_part3.py` holds the `host-hardening` component's content against the bytes
the role wrote. These tests hold what is specific to the last host: daniel-box ships the same
bytes, takes no i915 option, and retires what the role's absent arms removed. H7.48's tests hold
every host's package to the crons `crons.yml`'s absent arms removed, and each cluster node's to
`docker_install`'s teardown.

Run: uv run pytest hosts/lib/tests/test_box_initial_setup_part3.py
"""

import json

import pytest
from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from test_pi_initial_setup_part3 import BLACKLIST, BLACKLIST_WRITTEN, WRITTEN

from renderer.renderer_lib import host as host_mode

FILTER = "/etc/rsyslog.d/49-homelab-info-filter.conf"
I915 = "/etc/modprobe.d/i915.conf"
BASELINE_PATHS = {*WRITTEN, BLACKLIST, FILTER, I915, "/etc/issue", "/etc/issue.net"}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


@pytest.mark.parametrize("dst", sorted(WRITTEN))
def test_daniel_box_ships_the_bytes_the_role_wrote(package, dst):
    assert package.files[dst][0].decode() == WRITTEN[dst]


def test_daniel_box_ships_the_filter_and_adopts_the_blacklist(package):
    assert FILTER in package.files
    edits = [edit["line"] + "\n" for edit in package.lines if edit["path"] == BLACKLIST]
    assert "".join(edits) == BLACKLIST_WRITTEN


def test_the_has_igpu_gate_keeps_the_i915_option_off_daniel_box(package):
    spec = yaml_fast.safe_load(
        (REPO / "hosts" / "daniel-box" / "package.yml").read_text()
    )
    assert "host-hardening" in spec["components"]
    component = yaml_fast.safe_load(
        (REPO / "hosts" / "components" / "host-hardening" / "component.yml").read_text()
    )
    entry = next(f for f in component["files"] if f["dst"] == I915)
    assert entry["when"] == "has_igpu"
    assert "/etc/sudoers.d/10-timestamp" in package.files
    assert I915 not in package.files


def test_the_cert_expiry_credentials_retire_here(package):
    legacy = json.loads(package.files["/usr/lib/homelab/manifest.json"][0])["legacy"]
    assert {"path": "/etc/homelab/cert-expiry-webhook.env"} in legacy


def _legacy(host: str) -> list[dict]:
    package = host_mode.render_host(host)
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])["legacy"]


def _sys_user(host: str) -> str:
    return render_context(None, host, inventory=REPO_INVENTORY)["sys_user"]


@pytest.mark.parametrize("host", ["daniel-box", "daniel-pi", "daniel-server"])
def test_every_host_retires_the_crons_whose_absent_arms_left_crons_yml(host):
    user = _sys_user(host)
    legacy = _legacy(host)
    for name in (
        "TLS cert-expiry watch",
        "Daily secret rotation audit",
        "Weekly secret rotation (auto tier)",
    ):
        assert {"cron": name, "user": user} in legacy, name


@pytest.mark.parametrize("host", ["daniel-box", "daniel-pi", "daniel-server"])
def test_every_host_retires_the_ansible_log_cron(host):
    """H7.63 deleted crons.yml's last absent arm, so each host's legacy list removes the cron."""
    assert {"cron": "Clear ansible log file", "user": "root"} in _legacy(host)


@pytest.mark.parametrize("host", ["daniel-box", "daniel-server"])
def test_each_cluster_node_retires_the_docker_teardown_in_removal_order(host):
    user = _sys_user(host)
    teardown = [
        {"cron": "Redeploy ical-proxy", "user": user},
        {"cron": "Clean stale tdarr transcode cache", "user": user},
        # Reconcile removes a directory only once it is empty.
        {"path": "/etc/systemd/system/docker.service.d/20-go-runtime.conf"},
        {"path": "/etc/systemd/system/docker.service.d"},
        {"path": "/etc/systemd/system/containerd.service.d/20-go-runtime.conf"},
        {"path": "/etc/systemd/system/containerd.service.d"},
    ]
    assert [entry for entry in _legacy(host) if entry in teardown] == teardown
