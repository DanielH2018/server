"""daniel-box's package carries the deploy UI daemon with its in-flight panel (H7.50).

The daemon's behaviour has its own tests under `hosts/components/deploy-ui/tests/`. These hold what
the move added: the unit and its enable link, the port's ufw allows, and the legacy entries that hand
the Ansible-era unit, files and rules to the package.

Run: uv run pytest hosts/lib/tests/test_deploy_ui_component.py
"""

import json

import pytest
from _reconcile_arms import live_unit
from test_pi_initial_setup_part1 import seeded_pi

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

HOST = "daniel-box"
UNIT = "/usr/lib/systemd/system/deploy-ui.service"
DEFAULTS = yaml_fast.safe_load(
    (REPO / "hosts/components/deploy-ui/defaults/main.yml").read_text()
)
OLD_COMMENT = "deploy-ui from the k3s cluster"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host(HOST)


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


@pytest.fixture(scope="module")
def context() -> dict:
    return render_context(None, HOST, inventory=REPO_INVENTORY)


def _port_rules(manifest) -> list[dict]:
    port = f"port {DEFAULTS['deploy_ui_port']} "
    return [r for r in manifest["ufw"]["rules"] if port in r["spec"]]


def test_the_daemon_runs_from_the_package_as_the_operator_on_the_hosts_address(
    package, manifest, context
):
    unit = package.files[UNIT][0].decode()
    assert f"User={context['sys_user']}\n" in unit
    assert f"Environment=DEPLOY_UI_BIND={context['server_ip']}\n" in unit
    assert f"Environment=DEPLOY_UI_PORT={DEFAULTS['deploy_ui_port']}\n" in unit
    assert f"{DEFAULTS['deploy_ui_dir']}/deploy_ui.py\n" in unit
    assert f"{DEFAULTS['deploy_ui_dir']}/deploy_ui.py" in package.files
    assert "deploy-ui.service" in manifest["enable_links"]


def test_a_changed_python_file_restarts_the_daemon_is_clean(package, manifest):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Deploy
    code = [
        dst
        for dst in package.files
        if dst.startswith(DEFAULTS["deploy_ui_dir"] + "/") and dst.endswith(".py")
    ]
    assert f"{DEFAULTS['deploy_ui_dir']}/deploy_ui.py" in code
    assert {p: manifest["activations"].get(p) for p in code} == dict.fromkeys(
        code, "try-restart:deploy-ui.service"
    )


def test_the_port_admits_the_pod_cidr_and_both_nodes_and_never_the_lan(
    manifest, context
):
    nodes = {context["server_ip"], context["hostvars"]["daniel-server"]["server_ip"]}
    sources = {r["spec"].split()[2] for r in _port_rules(manifest)}
    assert sources == {context["k3s_pod_cidr"]} | nodes
    assert context["lan_subnet"] not in sources


def test_every_file_the_role_installed_is_handed_to_the_package(package, manifest):
    retired = {e["path"] for e in manifest["legacy"] if "path" in e}
    shipped = [
        dst.removeprefix(DEFAULTS["deploy_ui_dir"])
        for dst in package.files
        if dst.startswith(DEFAULTS["deploy_ui_dir"] + "/")
    ]
    assert {"/etc/systemd/system/deploy-ui.service"} <= retired
    no_longer_shipped = {
        f"/opt/deploy-ui/{name}"
        for name in ("gitops_hold.py", "gitops_ledger.py", "gitops_markers.py")
    }
    assert sorted(f"/opt/deploy-ui{rel}" for rel in shipped if rel) == sorted(
        p
        for p in retired
        if p.startswith("/opt/deploy-ui/") and p not in no_longer_shipped
    )
    assert no_longer_shipped <= retired


def test_reconcile_relabels_the_live_allows_rather_than_deleting_them(
    package, manifest, tmp_path
):
    node = seeded_pi(package, tmp_path)
    node.state["host"] = HOST
    node.state["installed"][f"homelab-{HOST}"] = node.state["installed"].pop(
        "homelab-daniel-pi"
    )
    node.state["users"] = {u["name"]: u["uid"] for u in manifest["users"]["users"]}
    # H7.60's filter restarts rsyslog, which the Pi fixture masks.
    live_unit(node, "rsyslog.service")
    declared = [r for r in manifest["ufw"]["rules"] if r.get("keep")]
    others = [r for r in manifest["ufw"]["rules"] if not r.get("keep")]
    port_rules = _port_rules(manifest)
    node.state["ufw"] = {
        "active": True,
        "rules": [[r["spec"], f"homelab:keep:{r['id']}"] for r in declared]
        + [
            [r["spec"], OLD_COMMENT if r in port_rules else f"homelab:{r['id']}"]
            for r in others
        ],
        "policy": {"incoming": "deny", "outgoing": "allow", "routed": "deny"},
        "fail": [],
    }

    result = node.run()

    assert result.returncode == 0, result.stderr
    live = [tuple(r) for r in node.state["ufw"]["rules"]]
    for r in port_rules:
        assert (r["spec"], f"homelab:{r['id']}") in live
    assert not [r for r in live if r[1] == OLD_COMMENT]
    ufw = [c["argv"][1:] for c in node.calls("ufw")]
    assert not [a for a in ufw if a[:1] == ["delete"]]
