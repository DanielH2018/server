"""daniel-server's `nut-secondary` component: the package-owned half of the UPS shutdown chain.

H4.3a added the declared `upsmon.conf` output, and H4.3c added its consumers. These tests hold
the output's mode, owner, `persist` flag and dependent, the absence of a symlink onto the
nut-client conffile, the upsd endpoint and the placeholder in the MONITOR line, and the `role:`
key that lets a component render the `nut_host` role's templates and defaults in place. They also
hold the nut-monitor drop-in and its boot order, the watchdog unit and its credential, the udev
rule, the legacy entries and the manifest rows reconcile reads.

Run: uv run pytest hosts/lib/tests/test_nut_secondary_component.py
"""

import json

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused

COMPONENT = REPO / "hosts" / "components" / "nut-secondary" / "component.yml"
TEMPLATE = "/usr/lib/homelab/secret-templates/etc/nut/upsmon.conf"
CONF = "/etc/nut/upsmon.conf"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-server")


def test_the_output_is_persisted_root_nut_0640_and_restarts_nut_monitor(package):
    [row] = [o for o in package.outputs if o["destination"] == "etc/nut/upsmon.conf"]
    assert (row["owner"], row["group"], row["mode"]) == ("root", "nut", "0640")
    assert row["persist"] is True
    assert row["dependents"] == ["nut-monitor.service"]
    assert row["keys"] == ["nut_monitor_password"]
    assert row["template"] == TEMPLATE


def test_no_symlink_lands_on_the_nut_client_conffile(package):
    # A link at /etc/nut/upsmon.conf would make nut-monitor read the file H4.3a leaves unread.
    assert CONF not in package.links
    assert CONF not in package.files


def test_the_monitor_line_names_the_loopback_upsd_and_a_placeholder(package):
    # R10-01: without `inventory_hostname` this renders `apc-ups@` with an empty host.
    lines = package.files[TEMPLATE][0].decode().splitlines()
    [monitor] = [line for line in lines if line.startswith("MONITOR ")]
    assert monitor.split()[1] == "apc-ups@127.0.0.1"
    assert "${nut_monitor_password}" in monitor


def test_the_package_lists_the_component():
    spec = yaml.safe_load((REPO / "hosts/daniel-server/package.yml").read_text())
    assert "nut-secondary" in spec["components"]
    assert yaml.safe_load(COMPONENT.read_text())["role"] == "nut_host"


def render_with(tmp_path, component: dict):
    """daniel-server rendered from a fixture `hosts/` tree holding only `component`."""
    root = tmp_path / "hosts"
    (root / "lib").mkdir(parents=True)
    (root / "components" / "fixture").mkdir(parents=True)
    (root / "components" / "fixture" / "component.yml").write_text(
        yaml.safe_dump(component)
    )
    (root / "daniel-server").mkdir()
    (root / "daniel-server" / "package.yml").write_text(
        yaml.safe_dump({"components": ["fixture"]})
    )
    return host_mode.render_host("daniel-server", root)


def test_a_role_outside_the_allowlist_is_refused(tmp_path):
    with pytest.raises(RenderRefused, match="role 'k3s' is not one of"):
        render_with(tmp_path, {"role": "k3s", "files": []})


def test_a_role_template_with_no_role_is_refused(tmp_path):
    output = {
        "src": "role:host-upsmon.conf.j2",
        "path": CONF,
        "owner": "root",
        "group": "nut",
        "mode": "0640",
        "keys": ["nut_monitor_password"],
    }
    component = {"outputs": [output]}
    with pytest.raises(RenderRefused, match="names a role, and it lists none"):
        render_with(tmp_path, component)


UNITS = "/usr/lib/systemd/system"
DROPIN = f"{UNITS}/nut-monitor.service.d/50-homelab.conf"
WATCHDOG_UNIT = f"{UNITS}/homelab-ups-watchdog.service"
TOKEN = "ups_secondary_daniel_server_push_token"
# The role's udev rule, pinned here because H4.3c deleted the Ansible tasks that held its text.
RULE = (
    'SUBSYSTEM=="usb", ATTR{idVendor}=="051d", ATTR{idProduct}=="0002", MODE="0666"\n'
)


def text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def boot_order_problems(dropin: str) -> list[str]:
    """What a nut-monitor drop-in lacks of the boot order, [] when it holds all of it."""
    lines = [line.strip() for line in dropin.splitlines()]
    wanted = (
        "Wants=homelab-secrets.service",
        "After=homelab-secrets.service",
        "Environment=NUT_CONFPATH=/var/lib/homelab-secrets/etc/nut",
        "EnvironmentFile=",
        "ExecStart=/lib/nut/upsmon -F",
        "ExecReload=/lib/nut/upsmon -c reload",
    )
    return [f"no {line}" for line in wanted if line not in lines] + [
        f"must not carry {line}" for line in lines if line.startswith("Requires=")
    ]


def credential_problems(unit: str, credentials: list[str]) -> list[str]:
    """What a unit's `LoadCredential=` lines get wrong against the package's credentials."""
    loaded = [
        line.partition("=")[2].partition(":")[2].removeprefix("/run/credstore/")
        for line in unit.splitlines()
        if line.startswith("LoadCredential=")
    ]
    return [
        f"{key} is not a package credential" for key in loaded if key not in credentials
    ] + [f"loads {key}, not {TOKEN}" for key in loaded if key != TOKEN]


def test_the_drop_in_points_upsmon_at_the_rendered_file_and_orders_after_secrets(
    package,
):
    assert boot_order_problems(text(package, DROPIN)) == []


def test_a_drop_in_with_requires_or_no_confpath_is_flagged():
    assert boot_order_problems("[Unit]\nRequires=homelab-secrets.service\n") == [
        "no Wants=homelab-secrets.service",
        "no After=homelab-secrets.service",
        "no Environment=NUT_CONFPATH=/var/lib/homelab-secrets/etc/nut",
        "no EnvironmentFile=",
        "no ExecStart=/lib/nut/upsmon -F",
        "no ExecReload=/lib/nut/upsmon -c reload",
        "must not carry Requires=homelab-secrets.service",
    ]


def test_the_watchdog_loads_daniel_servers_own_token(package):
    # R10-01: a render built without `inventory_hostname` names `ups_secondary_push_token`.
    unit = text(package, WATCHDOG_UNIT)
    assert f"LoadCredential=kuma_push_token:/run/credstore/{TOKEN}" in unit
    assert credential_problems(unit, package.credentials) == []


def test_a_unit_naming_the_other_hosts_token_is_flagged():
    unit = "LoadCredential=kuma_push_token:/run/credstore/ups_secondary_push_token\n"
    assert len(credential_problems(unit, [TOKEN])) == 2


def test_the_watchdog_ships_as_a_plain_package_script_on_a_timer(package):
    script = "/usr/lib/homelab/ups-secondary-health"
    assert "source /usr/lib/homelab/kuma-push-lib.sh" in text(package, script)
    assert package.files[script][1] == 0o755
    assert f"ExecStart={script}" in text(package, WATCHDOG_UNIT)
    assert "OnCalendar=*:0/10" in text(package, f"{UNITS}/homelab-ups-watchdog.timer")
    assert f"{UNITS}/timers.target.wants/homelab-ups-watchdog.timer" in package.links
    enable = yaml.safe_load(COMPONENT.read_text())["enable"]
    assert enable["homelab-ups-watchdog.timer"]["when"] == "nut_host_watchdog_armed"


def test_the_package_ships_the_apc_udev_rule_with_the_roles_text(package):
    assert text(package, "/usr/lib/udev/rules.d/99-homelab-nut-apc.rules") == RULE


def test_the_manifest_lists_nut_monitor_and_retires_the_ansible_entries(package):
    manifest = json.loads(text(package, "/usr/lib/homelab/manifest.json"))
    assert "nut-monitor.service" in manifest["units"]
    assert "homelab-ups-watchdog.timer" in manifest["enable_links"]
    # The claude-code component retires its own entries; the rest are the package's and nut's.
    claude = yaml.safe_load(
        (REPO / "hosts/components/claude-code/component.yml").read_text()
    )
    # Its one templated entry names daniel-server's sys_user.
    theirs = [
        json.dumps(e, sort_keys=True).replace("{{ sys_user }}", "ubuntu")
        for e in claude["legacy"]
    ]
    ours = [
        e for e in manifest["legacy"] if json.dumps(e, sort_keys=True) not in theirs
    ]
    assert ours == [
        {"path": "/etc/apt/sources.list.d/github-cli.list"},
        {"path": "/usr/local/bin/sops"},
        {"cron_file": "ups-secondary-health"},
        {"path": "/usr/local/bin/ups-secondary-health.sh"},
        {"path": "/etc/nut/kuma-push.env"},
        {"path": "/etc/udev/rules.d/99-nut-apc.rules"},
        {"path": "/var/lib/homelab/setup-render-manifest.d/nut-host"},
    ]
    assert "nut-client" in package.depends


def test_a_role_script_with_no_role_is_refused(tmp_path):
    files = [{"src": "role:ups-secondary-health.sh.j2", "dst": "/usr/lib/homelab/x"}]
    with pytest.raises(RenderRefused, match="names a role, and it lists none"):
        render_with(tmp_path, {"files": files})
