"""The rendered `registry-gc` component: the mirror registries' garbage collection (H7.64).

The script's behaviour is run against stubs in `ansible/tests/services/test_registry_gc_*.py`.
These tests hold what the move added: the units that run it, the two Job manifests in host mode,
the toggle, the copies of the registry role's keys, and the legacy entries.
"""

import dataclasses
import re

import pytest
import yaml

from lib.estate import role_defaults
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "registry-gc"
REGISTRY_ROLE = REPO / "ansible" / "roles" / "k8s" / "registry"
SCRIPT = "/usr/lib/homelab/registry-gc.sh"
UNITS = "/usr/lib/systemd/system"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-registry-gc.conf"
JOB_DIR = "/etc/rancher/k3s/manifests/registry-jobs"
GC_FILES = (
    SCRIPT,
    TMPFILES,
    f"{JOB_DIR}/ghcr-mirror-gc-job.yaml",
    f"{JOB_DIR}/dockerhub-mirror-gc-job.yaml",
    f"{UNITS}/homelab-registry-gc.service",
    f"{UNITS}/homelab-registry-gc.timer",
)
# Only the component's script reads these.
OWN_KEYS = frozenset(
    {
        "registry_gc_ghcr_mirror_warm_budget_s",
        "registry_gc_dockerhub_mirror_warm_budget_s",
    }
)
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-box")


def _text(package, dst: str) -> str:
    return package.files[dst][0].decode()


def test_the_package_ships_the_script_root_only_and_every_file(package):
    assert set(GC_FILES) <= set(package.files)
    assert package.files[SCRIPT][1] == 0o700
    assert package.files[f"{JOB_DIR}/ghcr-mirror-gc-job.yaml"][1] == 0o644


def test_the_service_runs_the_script_as_root_and_loads_the_ping_key(package):
    service = _text(package, f"{UNITS}/homelab-registry-gc.service")
    assert f"ExecStart={SCRIPT}\n" in service
    assert "User=" not in service
    assert "SyslogIdentifier=registry-gc\n" in service
    assert (
        "LoadCredential=healthchecks_ping_key:/run/credstore/healthchecks_ping_key\n"
        in service
    )
    assert "healthchecks_ping_key" in package.credentials
    assert f"--create {TMPFILES}\n" in service


def test_a_stop_leaves_the_exit_trap_time_to_restore_the_mirror(package):
    script = _text(package, SCRIPT)
    assert "trap scale_up_and_report EXIT" in script
    job_delete = re.search(
        r'delete job "\$MIRROR_JOB"[^\n]*\\\n[^\n]*--timeout=(\d+)s', script
    )
    rollout = re.search(r"rollout status [^\n]*--timeout=(\d+)s", script)
    assert job_delete and rollout
    service = _text(package, f"{UNITS}/homelab-registry-gc.service")
    (stop,) = re.findall(r"^TimeoutStopSec=(\d+)s$", service, re.MULTILINE)
    assert int(stop) > int(job_delete[1]) + int(rollout[1])


def test_the_script_reads_the_credential_and_the_packaged_check_lib(package):
    script = _text(package, SCRIPT)
    assert "source /usr/lib/homelab/homelab-check-lib.sh" in script
    assert '"$CREDENTIALS_DIRECTORY/healthchecks_ping_key"' in script
    assert "/etc/healthchecks" not in script


def test_the_tmpfiles_lines_keep_the_directories_modes(package):
    lines = _text(package, TMPFILES).splitlines()
    assert f"d {JOB_DIR} 0700 root root -" in lines
    assert "d /var/lib/registry-gc 0755 root root -" in lines


def test_the_timer_fires_at_the_schedule_the_keys_hold_and_is_enabled(package):
    keys = role_defaults(COMPONENT)
    day = WEEKDAYS[int(keys["registry_k8s_gc_cron_weekday"])]
    hour = int(keys["registry_k8s_gc_cron_hour"])
    minute = int(keys["registry_k8s_gc_cron_minute"])
    timer = _text(package, f"{UNITS}/homelab-registry-gc.timer")
    assert f"OnCalendar={day} *-*-* {hour:02d}:{minute:02d}:00\n" in timer
    assert "homelab-registry-gc.timer" in package.enable_links()


def test_reconcile_never_restarts_a_running_collection(package):
    assert package.policies["homelab-registry-gc.service"] == "manual"


def test_the_package_retires_the_cron_and_the_old_script(package):
    assert {"cron_file": "registry-gc"} in package.legacy
    assert {"path": "/usr/local/bin/registry-gc.sh"} in package.legacy


def test_the_toggle_drops_the_files_the_link_and_the_policy(tmp_path):
    root = tmp_path / "hosts"
    (root / "daniel-box").mkdir(parents=True)
    (tmp_path / "ansible").symlink_to(REPO / "ansible")
    (root / "components").mkdir()
    (root / "components" / "registry-gc").symlink_to(COMPONENT)
    (root / "lib").symlink_to(REPO / "hosts" / "lib")
    (root / "daniel-box" / "package.yml").write_text(
        yaml.safe_dump({"components": ["registry-gc"]})
    )
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    box = host_mode.REPO_INVENTORY.host_vars / "daniel-box.yml"
    body = {**yaml.safe_load(box.read_text()), "registry_k8s_manage_gc": False}
    (host_vars / "daniel-box.yml").write_text(yaml.safe_dump(body))
    inventory = dataclasses.replace(host_mode.REPO_INVENTORY, host_vars=host_vars)

    package = host_mode.render_host("daniel-box", root, inventory=inventory)

    assert not set(GC_FILES) & set(package.files)
    assert "homelab-registry-gc.timer" not in package.enable_links()
    assert "homelab-registry-gc.service" not in package.policies


def unequal_copies(copies: dict, originals: dict) -> list[str]:
    """Each key of `copies` outside `OWN_KEYS` whose value `originals` does not hold."""
    return sorted(
        key
        for key in set(copies) - OWN_KEYS
        if key not in originals or copies[key] != originals[key]
    )


def test_each_copied_key_equals_the_registry_roles():
    copies = role_defaults(COMPONENT)
    assert {"registry_k8s_image", "registry_k8s_gc_cron_hour"} <= set(copies)
    assert unequal_copies(copies, role_defaults(REGISTRY_ROLE)) == []


def test_a_copy_that_differs_from_the_role_is_flagged():
    copies = {
        "registry_k8s_gc_deadline_s": 1,
        "registry_gc_ghcr_mirror_warm_budget_s": 5,
    }
    originals = {"registry_k8s_gc_deadline_s": 1200}
    assert unequal_copies(copies, originals) == ["registry_k8s_gc_deadline_s"]
