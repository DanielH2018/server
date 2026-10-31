"""daniel-box's claude-code subjects ship in its host package, and the role skips them (H7.40, slice 1).

The component renders the claude_code role's templates in place, and the role still applies to
daniel-box for the subjects later slices move. These tests hold the two writers apart: every
role task that writes a template or file the package ships skips daniel-box through
`claude_code_packaged_hosts`, and the agent's uid comes from each host's own package.yml.

Run: uv run pytest hosts/lib/tests/test_claude_code_on_daniel_box.py
"""

import json
import re

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


def _local_import(task: dict) -> str | None:
    for key in ("ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
        target = task.get(key)
        if isinstance(target, dict):
            target = target.get("file")
        if isinstance(target, str) and "/" not in target:
            return target
    return None


def _sources(task: dict) -> list[str]:
    module = task.get("ansible.builtin.template") or task.get("ansible.builtin.copy")
    if not isinstance(module, dict) or module.get("remote_src") or "src" not in module:
        return []
    src = str(module["src"])
    loop = task.get("loop")
    if "{{ item }}" in src and isinstance(loop, list):
        return [src.replace("{{ item }}", str(item)) for item in loop]
    return [] if "{{" in src else [src]


def role_writers(
    tasks: list[dict], guarded: bool = False
) -> list[tuple[str, str, bool]]:
    """`(task name, src, guarded)` for each template or copy task reached from `tasks`.

    A task is guarded when its own `when:`, or that of an enclosing block or import, names
    `claude_code_packaged_hosts`.
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
        for src in _sources(task):
            found.append((task.get("name", ""), src, here))
    return found


def packaged_sources() -> set[str]:
    specs = [COMPONENT, REPO / "hosts" / "daniel-box" / "package.yml"]
    found = set()
    for spec in specs:
        for entry in yaml.safe_load(spec.read_text()).get("files") or []:
            src = str(entry.get("src", ""))
            for prefix in ("role:", "role-file:"):
                if src.startswith(prefix):
                    found.add(src.removeprefix(prefix))
    return found


def unguarded(writers: list[tuple[str, str, bool]], shipped: set[str]) -> list[str]:
    return sorted(
        f"{name}: {src}"
        for name, src, guarded in writers
        if src in shipped and not guarded
    )


def test_the_role_skips_on_daniel_box_every_file_the_package_ships():
    writers = role_writers(yaml.safe_load((ROLE / "tasks" / "main.yml").read_text()))
    shipped = packaged_sources()
    reached = {src for _, src, _ in writers} & shipped
    assert {
        "claude-rc.service.j2",
        "fleet-slice-caps.conf.j2",
        "login-slice-caps.conf.j2",
        "claude-clone-sync.sh",
        "claude-dotfiles-sync.timer.j2",
        "agent-gitconfig.j2",
        "homelab_rootq.py",
        "50-claude-stop-alert.rules.j2",
    } <= reached
    assert unguarded(writers, shipped) == []


TASK = {"name": "Install it", "ansible.builtin.template": {"src": "x.j2", "dest": "/x"}}
SKIP_BOX = f"inventory_hostname not in {GUARD}['x']"


def test_a_role_task_writing_a_packaged_file_without_the_guard_is_reported():
    assert unguarded(role_writers([TASK]), {"x.j2"}) == ["Install it: x.j2"]


def test_a_guard_on_the_task_or_its_enclosing_block_is_clean():
    assert unguarded(role_writers([{**TASK, "when": SKIP_BOX}]), {"x.j2"}) == []
    block = {"name": "Both", "when": [SKIP_BOX], "block": [TASK]}
    assert unguarded(role_writers([block]), {"x.j2"}) == []


def test_the_packaged_map_names_daniel_box_for_exactly_the_slice_one_subjects():
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
        )
    }
