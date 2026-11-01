"""daniel-box's claude-code subjects ship in its host package (H7.40, slices 1-4).

The component renders the claude_code role's templates in place, and the role ends on every
host, so the package is the one writer. These tests hold each slice's subjects in the package
and the agent's uid to each host's own package.yml.

Run: uv run pytest hosts/lib/tests/test_claude_code_on_daniel_box.py
"""

import json
import re
import shutil

import pytest
import yaml

from lib.estate import Inventory
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

ROLE = REPO / "ansible" / "roles" / "setup" / "claude_code"
COMPONENT = REPO / "hosts" / "components" / "claude-code" / "component.yml"
UNITS = "/usr/lib/systemd/system"
# A member of each subject the slice moves, so an emptied component cannot pass.
SLICE_ONE = (
    f"{UNITS}/homelab-claude-rc.service",
    f"{UNITS}/homelab-claude-rc-restart.timer",
    f"{UNITS}/homelab-claude-bg-resume.service",
    f"{UNITS}/homelab-claude-cgroup-metrics.timer",
    f"{UNITS}/homelab-claude-clone-sync.timer",
    f"{UNITS}/homelab-claude-dotfiles-sync.timer",
    f"{UNITS}/homelab-claude-agent.service",
    f"{UNITS}/user.slice.d/homelab-claude-fleet-caps.conf",
    f"{UNITS}/claude-stop-alert@.service",
    f"{UNITS}/fanout-alert@.service",
    f"{UNITS}/homelab-rootq.socket",
    "/etc/sudoers.d/homelab-rootq",
    "/usr/lib/homelab/secret-templates/var/lib/claude/.config/gh/hosts.yml",
)
# The second slice: the lander, the server-batch lander, the pipeline timeline, the memory sync
# and the takeover check's wrapper and grant. Each is armed only in daniel-box's host_vars.
SLICE_TWO = (
    f"{UNITS}/homelab-claude-land@.service",
    "/usr/share/polkit-1/rules.d/50-claude-land.rules",
    "/etc/claude-land/approval-paths",
    f"{UNITS}/homelab-claude-fanout-server-lander.timer",
    f"{UNITS}/homelab-claude-pipeline-timeline.timer",
    f"{UNITS}/homelab-claude-memory-sync.timer",
    "/usr/local/sbin/homelab-takeover-check",
    "/etc/sudoers.d/homelab-takeover-check",
)
# The third slice: triage, the agent's home and the homelab-ui browser toolchain.
SLICE_THREE = (
    f"{UNITS}/claude-triage@.service",
    f"{UNITS}/claude-triage-report-alert@.service",
    f"{UNITS}/homelab-claude-triage-poll.timer",
    "/usr/share/polkit-1/rules.d/50-claude-triage.rules",
    "/opt/claude-triage/claude_triage_run.py",
    "/usr/lib/homelab/secret-templates/etc/claude-triage/oauth-token",
    "/usr/lib/homelab/claude-agent/ssh-config",
    "/usr/lib/homelab/secret-templates/var/lib/claude/.config/homelab-ui/credentials.json",
    f"{UNITS}/homelab-claude-agent-browser.service",
)
# The fourth slice: the artifacts node's peer sync and OTLP-door heartbeat, with the directories
# the artifacts pod mounts. They ship on the node only.
SLICE_FOUR = (
    "/usr/lib/homelab/sync-artifacts",
    "/usr/lib/homelab/claude-otel-health",
    f"{UNITS}/homelab-sync-artifacts.service",
    f"{UNITS}/homelab-sync-artifacts.timer",
    f"{UNITS}/homelab-claude-otel-health.service",
    f"{UNITS}/homelab-claude-otel-health.timer",
    "/usr/lib/tmpfiles.d/homelab-artifacts.conf",
)
AGENT_UID = {"daniel-box": 996, "daniel-server": 995}


@pytest.fixture(scope="module")
def box() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


@pytest.fixture(scope="module")
def server() -> host_mode.Package:
    return host_mode.render_host("daniel-server")


def slice_caps(package) -> set[str]:
    return {
        m.group(1)
        for path in package.files
        if (
            m := re.fullmatch(
                rf"{UNITS}/(user-\d+)\.slice\.d/homelab-claude-caps\.conf", path
            )
        )
    }


def test_daniel_box_ships_each_slice_one_subject(box):
    assert [path for path in SLICE_ONE if path not in box.files] == []
    legacy = {entry.get("unit") for entry in box.legacy}
    assert {"claude-rc.service", "claude-cgroup-metrics.timer"} <= legacy


def test_daniel_box_ships_each_slice_two_subject_and_retires_its_ansible_copy(box):
    assert [path for path in SLICE_TWO if path not in box.files] == []
    # The packaged units page through homelab-alert@, which the failure-alert component ships.
    assert f"{UNITS}/homelab-alert@.service" in box.files
    legacy = {entry.get("unit") for entry in box.legacy}
    assert {
        "claude-land@.service",
        "claude-fanout-server-lander.timer",
        "claude-pipeline-timeline.timer",
        "claude-memory-sync.timer",
    } <= legacy
    retired = {entry.get("path") for entry in box.legacy}
    assert {
        "/etc/polkit-1/rules.d/50-claude-land.rules",
        "/etc/sudoers.d/claude-takeover-check",
    } <= retired


def test_daniel_server_ships_no_slice_two_subject(server):
    assert [path for path in SLICE_TWO if path in server.files] == []


def test_daniel_box_ships_each_slice_three_subject_and_retires_its_ansible_copy(box):
    assert [path for path in SLICE_THREE if path not in box.files] == []
    legacy = {entry.get("unit") for entry in box.legacy}
    assert {
        "claude-triage-poll.timer",
        "claude-triage-alert.service",
        "claude-triage-poll-alert.service",
    } <= legacy
    retired = {entry.get("path") for entry in box.legacy}
    assert {
        "/etc/polkit-1/rules.d/50-claude-triage.rules",
        "/var/cache/claude-agent-node",
    } <= retired


def test_daniel_box_ships_each_slice_four_subject(box):
    assert [path for path in SLICE_FOUR if path not in box.files] == []


def test_daniel_server_ships_none_of_the_artifacts_node_timers(server):
    assert [path for path in SLICE_FOUR if path in server.files] == []


def test_neither_host_ships_the_retired_flux_download_oneshot(box, server):
    retired = (
        "/usr/lib/homelab/takeover-check-flux",
        f"{UNITS}/homelab-takeover-check-flux.service",
    )
    for package in (box, server):
        assert [path for path in retired if path in package.files] == []
        assert not [p for p in package.links if p.endswith(retired[1].rsplit("/")[-1])]


def test_the_takeover_check_runs_homelab_tools_flux_and_retires_its_own_copy(box):
    wrapper = box.files["/usr/local/sbin/homelab-takeover-check"][0].decode()
    assert "FLUX=/usr/lib/homelab/bin/flux\n" in wrapper
    assert "homelab-tools" in box.depends
    # Reconcile only rmdirs a directory, so each file the copy held is retired ahead of it.
    lib = "/usr/local/lib/homelab-takeover-check"
    paths = [e["path"] for e in box.legacy if e.get("path", "").startswith(lib)]
    assert paths == [f"{lib}/flux", f"{lib}/flux.sha256", lib]


def test_daniel_server_ships_the_browser_only_to_take_it_back(server):
    # Triage, the peer config and the homelab-ui login are daniel-box's alone. The browser unit
    # ships wherever the agent exists, and with homelab-ui off its script removes the toolchain.
    browser = f"{UNITS}/homelab-claude-agent-browser.service"
    assert [p for p in SLICE_THREE if p in server.files] == [browser]
    script = server.files["/usr/lib/homelab/claude-agent-browser"][0].decode()
    assert "mcp remove" in script and "npm install" not in script


SETUP = "/usr/lib/homelab/claude-agent-setup"
UI_LOGIN_REMOVAL = 'drop "$home/.config/homelab-ui/credentials.json"'


def test_a_host_with_homelab_ui_off_removes_the_agents_login(box, server):
    assert UI_LOGIN_REMOVAL not in box.files[SETUP][0].decode()
    assert UI_LOGIN_REMOVAL in server.files[SETUP][0].decode()


def test_the_package_installs_no_link_into_the_agents_home(box, server):
    # The agent owns its home, so claude-agent-setup links each login there after checking the
    # path. A link the package installed would put root's write inside that directory.
    for package in (box, server):
        assert [p for p in package.links if p.startswith("/var/lib/claude/")] == []


def test_each_login_renders_where_the_setup_script_links_to_it(box, server):
    gh = "var/lib/claude/.config/gh/hosts.yml"
    for package, destinations in (
        (box, {"claude-agent/homelab-ui-credentials.json", gh}),
        (server, {gh}),
    ):
        rows = json.loads(package.files["/usr/lib/homelab/secrets.json"][0])["outputs"]
        assert {
            r["destination"] for r in rows if r["owner"] == "claude"
        } == destinations
        setup = package.files[SETUP][0].decode()
        for destination in destinations:
            assert f"/run/homelab-secrets/{destination}" in setup


@pytest.mark.parametrize("host", sorted(AGENT_UID))
def test_every_packaged_agent_takes_the_dotfiles_so_no_operator_config_copy_is_owed(
    host,
):
    # The package ships no copy of the operator config subset: the role runs it only for an
    # agent without the dotfiles. A host that turned them off would lose its subset silently.
    host_vars = yaml.safe_load(
        (REPO / "ansible" / "inventory" / "host_vars" / f"{host}.yml").read_text()
    )
    assert host_vars["claude_code_agent_dotfiles"] is True


def test_the_key_host_names_each_peer_whose_login_is_on(box):
    config = box.files["/usr/lib/homelab/claude-agent/ssh-config"][0].decode()
    assert re.findall(r"^Host (\S+)$", config, re.M) == ["daniel-server"]


def test_the_agents_profile_hands_landings_to_the_unit_the_box_ships(box):
    (profile,) = [p for p in box.files if p.endswith("/claude-agent/profile")]
    text = box.files[profile][0].decode()
    (unit,) = re.findall(r"^export LAND_HANDOFF_UNIT=(\S+)$", text, re.M)
    assert f"{UNITS}/{unit}@.service" in box.files


def test_daniel_box_declares_the_agent_with_its_own_uid(box):
    users = json.loads(box.files["/usr/lib/homelab/manifest.json"][0])["users"]["users"]
    assert {u["name"]: u["uid"] for u in users}["claude"] == 996
    assert 'u claude 996 "Claude agent" /var/lib/claude /bin/bash\n' in (
        box.files["/usr/lib/sysusers.d/homelab.conf"][0].decode()
    )


def test_the_rc_host_binds_the_runtime_dir_of_the_uid_the_package_declares(box):
    # claude_code_user_uid sits in host_vars and the uid in package.yml; the bind follows one.
    users = json.loads(box.files["/usr/lib/homelab/manifest.json"][0])["users"]["users"]
    uid = {u["name"]: u["uid"] for u in users}["claude"]
    unit = box.files[f"{UNITS}/homelab-claude-rc.service"][0].decode()
    assert re.findall(r"^BindPaths=(\S+)$", unit, re.M) == [f"/run/user/{uid}"]
    assert f"Environment=XDG_RUNTIME_DIR=/run/user/{uid}\n" in unit


def test_the_rc_host_rolled_back_to_the_operator_reaches_the_operators_bus(tmp_path):
    # The rollback sets claude_code_user to ubuntu and leaves claude_code_user_uid at the
    # agent's 996, so the unit must take the operator's uid from claude_code_login_uid.
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO / "ansible" / "inventory" / "host_vars", host_vars)
    path = host_vars / "daniel-box.yml"
    path.write_text(
        yaml.safe_dump(
            {**yaml.safe_load(path.read_text()), "claude_code_user": "ubuntu"}
        )
    )
    rolled_back = host_mode.render_host(
        "daniel-box", inventory=Inventory(host_vars=host_vars)
    )
    unit = rolled_back.files[f"{UNITS}/homelab-claude-rc.service"][0].decode()
    assert re.findall(r"^Environment=(XDG_RUNTIME_DIR=\S+)$", unit, re.M) == [
        "XDG_RUNTIME_DIR=/run/user/1000"
    ]
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n" in unit


def test_each_host_caps_its_own_agents_slice_and_the_operator_only_where_listed(
    box, server
):
    # daniel-box's host_vars take uid 1000 off claude_code_login_uids; daniel-server keeps it.
    assert slice_caps(box) == {"user-996"}
    assert slice_caps(server) == {"user-995", "user-1000"}


@pytest.mark.parametrize("host", sorted(AGENT_UID))
def test_each_hosts_users_entry_names_its_live_uid_and_follows_the_agent_toggle(host):
    # The users arm takes no `when:`, so switching the agent off means deleting the entry.
    spec = yaml.safe_load((REPO / "hosts" / host / "package.yml").read_text())
    host_vars = yaml.safe_load(
        (REPO / "ansible" / "inventory" / "host_vars" / f"{host}.yml").read_text()
    )
    declared = {u["name"]: u["uid"] for u in spec.get("users") or []}
    assert host_vars["claude_code_agent_user_enabled"] == ("claude" in declared)
    assert declared.get("claude") == AGENT_UID[host]
    assert "users" not in yaml.safe_load(COMPONENT.read_text())


TEXTFILES = "/var/lib/node-exporter-textfile"
# Each timer toggle, and the textfiles its timer writes that disarming it must remove.
TIMER_TEXTFILES = {
    "claude_code_fanout_server_lander_enabled": {
        f"{TEXTFILES}/fanout_operator_waits.prom",
        f"{TEXTFILES}/claude-fanout-server-lander.prom",
    },
    "claude_code_pipeline_timeline_enabled": {
        f"{TEXTFILES}/claude-pipeline-timeline.prom",
    },
}


def retired_textfiles(package) -> set[str]:
    return {
        e["path"] for e in package.legacy if e.get("path", "").startswith(TEXTFILES)
    }


@pytest.mark.parametrize("toggle", sorted(TIMER_TEXTFILES))
def test_disarming_a_timer_retires_its_textfiles(tmp_path, toggle):
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO / "ansible" / "inventory" / "host_vars", host_vars)
    path = host_vars / "daniel-box.yml"
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), toggle: False}))

    off = host_mode.render_host("daniel-box", inventory=Inventory(host_vars=host_vars))

    assert retired_textfiles(off) == TIMER_TEXTFILES[toggle]
