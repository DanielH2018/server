"""daniel-box's claude-code subjects ship in its host package, and the role skips them (H7.40, slices 1-3).

The component renders the claude_code role's templates in place, and the role still applies to
daniel-box for the subjects later slices move. These tests hold the two writers apart: every
role task that writes a template or file the package ships, or a path the package retires,
skips daniel-box through `claude_code_packaged_hosts`, and the agent's uid comes from each
host's own package.yml.

Run: uv run pytest hosts/lib/tests/test_claude_code_on_daniel_box.py
"""

import json
import re
from dataclasses import dataclass

import pytest
import yaml

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

ROLE = REPO / "ansible" / "roles" / "setup" / "claude_code"
COMPONENT = REPO / "hosts" / "components" / "claude-code" / "component.yml"
UNITS = "/usr/lib/systemd/system"
GUARD = "claude_code_packaged_hosts"
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


# ── the role skips what the package ships ───────────────────────────────────────────────────


DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
# The common task files the role imports, and the path each writes from the caller's vars.
COMMON_WRITES = {
    "common/tasks/alert_unit.yml": lambda v: (
        f"{v['alert_unit_env_dir']}/alert-webhook.env"
    ),
    "common/tasks/stamp_render.yml": lambda v: (
        f"/var/lib/homelab/setup-render-manifest.d/{v['stamp_render_name']}"
    ),
    "common/tasks/stamp_deployed.yml": lambda v: (
        f"/var/lib/homelab/setup-deployed-manifest.d/{v['stamp_deployed_name']}"
    ),
}
WRITE_MODULES = ("template", "copy", "file")


@dataclass(frozen=True)
class Write:
    task: str
    src: str
    dest: str
    guarded: bool


def _resolve(text: str, item: object = None) -> str:
    """`text` with `{{ item }}` and each `{{ <role default> }}` filled in, other braces kept."""
    if item is not None:
        text = text.replace("{{ item }}", str(item))
    return re.sub(
        r"\{\{ (\w+) \}\}", lambda m: str(DEFAULTS.get(m.group(1), m.group(0))), text
    )


def _local_import(task: dict) -> str | None:
    for key in ("ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
        target = task.get(key)
        if isinstance(target, dict):
            target = target.get("file")
        if isinstance(target, str) and "/" not in target:
            return target
    return None


def _writes(task: dict, guarded: bool) -> list[Write]:
    name = task.get("name", "")
    target = str(task.get("ansible.builtin.import_tasks", ""))
    for common, dest in COMMON_WRITES.items():
        if target.endswith(common):
            return [Write(name, "", _resolve(dest(task.get("vars") or {})), guarded)]
    modules = [task.get(f"ansible.builtin.{m}") for m in WRITE_MODULES]
    module = next((m for m in modules if isinstance(m, dict)), None)
    if module is None:
        return []
    loop = task.get("loop")
    found = []
    for item in loop if isinstance(loop, list) else [None]:
        src = (
            ""
            if module.get("remote_src")
            else _resolve(str(module.get("src", "")), item)
        )
        dest = _resolve(str(module.get("dest", module.get("path", ""))), item)
        found.append(Write(name, "" if "{{" in src else src, dest, guarded))
    return found


def role_writers(tasks: list[dict], guarded: bool = False) -> list[Write]:
    """Each template, copy, file or common-file write reached from `tasks`.

    A write is guarded when its task's own `when:`, or that of an enclosing block or import,
    names `claude_code_packaged_hosts`.
    """
    found = []
    for task in tasks:
        here = guarded or GUARD in str(task.get("when", ""))
        if "block" in task:
            found += role_writers(task["block"], here)
        elif (name := _local_import(task)) is not None:
            found += role_writers(
                yaml.safe_load((ROLE / "tasks" / name).read_text()), here
            )
        found += _writes(task, here)
    return found


def packaged_sources() -> set[str]:
    specs = [COMPONENT, REPO / "hosts" / "daniel-box" / "package.yml"]
    found = set()
    for spec in specs:
        loaded = yaml.safe_load(spec.read_text())
        for entry in (loaded.get("files") or []) + (loaded.get("outputs") or []):
            src = str(entry.get("src", ""))
            for prefix in ("role:", "role-file:"):
                if src.startswith(prefix):
                    found.add(src.removeprefix(prefix))
    return found


def unguarded(writers: list[Write], shipped: set[str]) -> list[str]:
    return sorted(
        f"{w.task}: {w.src}" for w in writers if w.src in shipped and not w.guarded
    )


def retired_writes(writers: list[Write], retired: set[str]) -> list[str]:
    """Each unguarded write at, or under, a path the package retires."""
    return sorted(
        f"{w.task}: {w.dest}"
        for w in writers
        if not w.guarded
        and any(w.dest == path or w.dest.startswith(f"{path}/") for path in retired)
    )


def main_writers() -> list[Write]:
    return role_writers(yaml.safe_load((ROLE / "tasks" / "main.yml").read_text()))


def test_the_role_skips_on_daniel_box_every_file_the_package_ships():
    writers = main_writers()
    shipped = packaged_sources()
    reached = {w.src for w in writers} & shipped
    assert {
        "claude-rc.service.j2",
        "fleet-slice-caps.conf.j2",
        "login-slice-caps.conf.j2",
        "claude-clone-sync.sh",
        "claude-dotfiles-sync.timer.j2",
        "agent-gitconfig.j2",
        "homelab_rootq.py",
        "50-claude-stop-alert.rules.j2",
        "claude-land@.service.j2",
        "claude-memory-sync.timer.j2",
        "claude-pipeline-timeline.service.j2",
        "claude-takeover-check.sudoers.j2",
        "claude-triage@.service.j2",
        "claude-triage-oauth-token.j2",
        "agent-ssh-config.j2",
        "agent-homelab-ui-credentials.json.j2",
    } <= reached
    assert unguarded(writers, shipped) == []


def test_the_role_writes_nothing_on_daniel_box_that_its_package_retires(box):
    writers = main_writers()
    retired = {entry["path"] for entry in box.legacy if "path" in entry}
    # The webhooks and the stamps reach the census only through the common imports.
    assert {"/etc/claude-rc", "/etc/claude-code-alerts"} <= retired
    assert retired_writes(writers, retired) == []
    webhook = "alert-webhook" + ".env"
    assert {
        f"/etc/claude-rc/{webhook}",
        "/etc/claude-rc",
        "/var/lib/homelab/setup-render-manifest.d/claude-code-ansible",
        f"/etc/claude-triage/{webhook}",
    } <= {w.dest for w in writers}


TASK = {"name": "Install it", "ansible.builtin.template": {"src": "x.j2", "dest": "/x"}}
SKIP_BOX = f"inventory_hostname not in {GUARD}['x']"
ALERT = {
    "name": "Page it",
    "ansible.builtin.import_tasks": "{{ role_path }}/../common/tasks/alert_unit.yml",
    "vars": {"alert_unit_env_dir": "/etc/old"},
}


def test_an_unguarded_alert_writing_under_a_retired_directory_is_reported():
    assert retired_writes(role_writers([ALERT]), {"/etc/old"}) == [
        "Page it: /etc/old/alert-webhook" + ".env"
    ]


def test_a_guarded_alert_or_one_outside_every_retired_path_is_clean():
    guarded = {**ALERT, "when": SKIP_BOX}
    assert retired_writes(role_writers([guarded]), {"/etc/old"}) == []
    assert retired_writes(role_writers([ALERT]), {"/etc/older"}) == []


def test_a_role_task_writing_a_packaged_file_without_the_guard_is_reported():
    assert unguarded(role_writers([TASK]), {"x.j2"}) == ["Install it: x.j2"]


def test_a_guard_on_the_task_or_its_enclosing_block_is_clean():
    assert unguarded(role_writers([{**TASK, "when": SKIP_BOX}]), {"x.j2"}) == []
    block = {"name": "Both", "when": [SKIP_BOX], "block": [TASK]}
    assert unguarded(role_writers([block]), {"x.j2"}) == []


def test_the_packaged_map_names_daniel_box_for_exactly_the_moved_subjects():
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert defaults[GUARD] == {
        subject: ["daniel-box"]
        for subject in (
            "remote_control",
            "bg_resume",
            "cgroup_metrics",
            "caps",
            "alerts",
            "rootq",
            "agent",
            "lander",
            "server_lander",
            "pipeline_timeline",
            "memory_sync",
            "takeover_check",
            "triage",
            "agent_home",
        )
    }
