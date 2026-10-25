"""The `k3s-restart` component as daniel-box's package renders it.

H6.3b: the unit, the timer, the credential, the lock's `tmpfiles.d` entry and the two files the
unit runs. The package needs `k3s_readonly_token` in `secrets.yml` and a daniel-box SOPS file to
render, so the fixtures below stand in for both; the operator adds the real ones.
"""

import json
import re

import jinja2
import pytest
import yaml
from deployer_lib import deploy_locks

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_sources import lib_source

# Every key the daniel-box package names: a credential outside secrets.yml fails the render.
_KEYS = (
    "k3s_readonly_token: x\n"
    "docs_refresh_push_token: x\n"
    "homelab_eval_push_token: x\n"
    "anthropic_api_key: x\n"
    "secret_rotation_push_token: x\n"
    "loki_route_witness_push_token: x\n"
    "ups_secondary_push_token: x\n"
    "nut_monitor_password: x\n"
    "homelab_builder_github_token: x\n"
    "gitops_deploy_discord_webhook: x\n"
)

COMPONENT = REPO / "hosts/components/k3s-restart"
SYSTEM = "/usr/lib/systemd/system/"
SERVICE = f"{SYSTEM}homelab-k3s-restart.service"
TIMER = f"{SYSTEM}homelab-k3s-restart.timer"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-k3s-restart.conf"


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    root = tmp_path_factory.mktemp("box")
    (root / "secrets.yml").write_text(_KEYS)
    (root / "sops").mkdir()
    (root / "sops/daniel-box.sops.yaml").write_text(_KEYS)
    return host_mode.render_host(
        "daniel-box", secrets=root / "secrets.yml", sops_dir=root / "sops"
    )


def text(package, dst: str) -> str:
    return package.files[dst][0].decode()


def test_the_service_loads_the_credential_in_h4s_form_after_the_secrets_unit(package):
    unit = text(package, SERVICE)
    assert "LoadCredential=k3s_readonly_token:/run/credstore/k3s_readonly_token" in unit
    assert "Wants=homelab-secrets.service" in unit
    assert "After=homelab-secrets.service" in unit


def test_the_service_carries_the_marker_the_timeout_and_the_stop_hook(package):
    unit = text(package, SERVICE)
    for line in (
        "Type=oneshot",
        "TimeoutStartSec=7min",
        "RuntimeDirectory=homelab-k3s-restart",
        "SyslogLevel=notice",
    ):
        assert line in unit.splitlines()
    assert re.search(r"^ExecStopPost=.* --stop-post ", unit, re.M)


def test_daniel_box_takes_the_deploy_lock_and_skips_the_server_restart_guard(package):
    [start] = re.findall(r"^ExecStart=(.*)$", text(package, SERVICE), re.M)
    assert start.startswith("/usr/lib/homelab/homelab-k3s-restart ")
    assert "--deploy-lock" in start
    assert "--guard-server-restart" not in start
    assert "--unit k3s.service" in start
    assert "--host daniel-box" in start


def test_an_agent_host_render_adds_the_guard_and_drops_the_lock(package):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    template = env.from_string(
        (COMPONENT / "units/homelab-k3s-restart.service.j2").read_text()
    )
    defaults = yaml.safe_load((COMPONENT / "component.yml").read_text())["defaults"]
    agent = template.render(
        **{
            **defaults,
            "inventory_hostname": "daniel-server",
            "k3s_restart_unit": "k3s-agent.service",
            "k3s_restart_deploy_lock": False,
            "k3s_restart_guard_server_restart": True,
        }
    )
    [start] = re.findall(r"^ExecStart=(.*)$", agent, re.M)
    assert "--guard-server-restart" in start
    assert "--deploy-lock" not in start


def test_the_timer_carries_the_window_and_the_two_accuracy_settings(package):
    timer = text(package, TIMER).splitlines()
    assert "OnCalendar=Mon..Fri *-*-02..31 11:32:00" in timer
    assert "AccuracySec=1s" in timer
    assert "RandomizedDelaySec=0" in timer


def test_a_host_without_a_window_fails_the_render():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    template = env.from_string(
        (COMPONENT / "units/homelab-k3s-restart.timer.j2").read_text()
    )
    with pytest.raises(jinja2.UndefinedError, match="k3s_restart_window"):
        template.render(inventory_hostname="x", k3s_restart_unit="k3s.service")


def test_the_timer_is_enabled_from_timers_target(package):
    link = f"{SYSTEM}timers.target.wants/homelab-k3s-restart.timer"
    # The UPS watchdog timer (H7.33) is a second enable link, so only this one is held.
    assert package.links[link] == "../homelab-k3s-restart.timer"


def test_the_credential_and_the_secrets_unit_are_in_the_package(package):
    assert package.secrets
    assert "k3s_readonly_token" in package.credentials


def test_the_package_ships_the_script_and_the_gates_side_by_side(package):
    for name in ("homelab-k3s-restart", "k3s_gates.py"):
        body, mode = package.files[f"/usr/lib/homelab/{name}"]
        assert body == (REPO / "hosts/lib" / name).read_bytes()
        assert mode == 0o755


def test_the_lock_entry_equals_the_deployers_path_through_the_var_lock_link(package):
    [line] = [
        raw.split()
        for raw in text(package, TMPFILES).splitlines()
        if raw.startswith("f ")
    ]
    kind, path, mode, owner, group, age = line
    lock = deploy_locks.lock_path(deploy_locks.SERVICE_LOCK_ALL)
    assert lock.replace("/var/lock/", "/run/lock/", 1) == path
    assert (kind, mode, age) == ("f", "0644", "-")
    assert owner == group


def test_the_lock_entry_owner_is_the_user_the_deployer_runs_as(package):
    owner = text(package, TMPFILES).split("server-deploy-all.lock")[1].split()[1]
    unit = (
        REPO / "ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2"
    ).read_text()
    assert re.search(r"^User=\{\{ sys_user \}\}$", unit, re.M)
    assert (
        owner
        == yaml.safe_load((REPO / "ansible/inventory/group_vars/all.yml").read_text())[
            "sys_user"
        ]
    )


def test_the_manifest_lists_the_unit_so_reconcile_manages_it(package):
    manifest = json.loads(text(package, "/usr/lib/homelab/manifest.json"))
    assert "homelab-k3s-restart.service" in manifest["units"]
    assert "homelab-k3s-restart.timer" in manifest["units"]


@pytest.mark.parametrize(
    "name", ["k3s_gates.py", "reconcile_lib/ufw.py", "homelab-reconcile"]
)
def test_a_top_level_module_is_a_valid_lib_entry(name):
    assert lib_source(REPO / "hosts/lib", name).is_file()


@pytest.mark.parametrize(
    "name", ["../x.py", "K3s.py", "a/b/c.py", "/etc/passwd", "x.sh"]
)
def test_a_lib_entry_outside_the_grammar_is_refused(name):
    with pytest.raises(RenderRefused):
        lib_source(REPO / "hosts/lib", name)
