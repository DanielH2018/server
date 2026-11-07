"""daniel-server's dev tooling moves into its package, and `/usr/bin/uv` reaches both cluster hosts (H7.61).

The `uv` and `vale` components put the homelab-tools binaries on PATH, the /usr/local/bin copies
that sit ahead of them are retired on this host, and `initial_setup` stops writing what the package
now owns here. The worktree holder scan's half of the row is
`ansible/tests/setup/test_worktree_holders_install.py`.

Run: uv run pytest hosts/lib/tests/test_server_dev_tooling.py
"""

import json

import jinja2
import pytest

from deploy_tools.setup_lib.setup_gates import packaged_guard_hosts
from lib import yaml_fast
from lib.estate import role_defaults
from lib.repo_paths import ANSIBLE
from renderer.renderer_lib import host as host_mode

ROLE = ANSIBLE / "roles" / "setup" / "initial_setup"
HOST = "daniel-server"
# Each subject's tasks, by file. Named so that a renamed task fails here rather than dropping out.
GUARDED = {
    "dev_tooling": {
        "host-basics.yml": {
            "Create the connecting user's installers directory",
            "Download the pinned uv installer",
            "Read the uv version this host already has",
            "Install uv (per-user; PEP 668-safe on 24.04+)",
            "Install prek as a uv tool",
            "Install the pinned host Python",
            "Look for the old uv symlink at the stable path",
            "Remove the old uv symlink so a real copy can replace it",
            "Publish uv at a stable path for systemd and cron",
        },
        "system-tuning.yml": {"Install Git hooks"},
    },
    "vale": {
        "host-basics.yml": {
            "Pin the Vale version this host installs",
            "Read the Vale version this host already has",
            "Install the pinned Vale binary",
        },
    },
    "worktree_holders": {
        "crons.yml": {
            "Create the directory the worktree holder scan installs into",
            "Install the netcat the shell worktree removal asks the holder scan with",
            "Install the root-run worktree holder scan a worktree removal asks first",
            "Name the agent users granted the worktree holder scan",
            "List the worktree holder scan's users and the checkout each one works in",
            "Map each user of the worktree holder scan to the worktree root it reports under",
            "Create the group that may connect to the worktree holder scan's socket",
            "Let sys_user connect to the worktree holder scan's socket",
            "Install the worktree holder scan's socket and its per-connection service",
            "Listen on the worktree holder scan's socket",
            "Record the deployed worktree holder scan",
        },
    },
}
GUARDED_TASKS = [
    (subject, name, task)
    for subject, files in GUARDED.items()
    for name, tasks in files.items()
    for task in sorted(tasks)
]


@pytest.fixture(scope="module")
def server() -> host_mode.Package:
    return host_mode.render_host(HOST)


@pytest.fixture(scope="module")
def box() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def _legacy_paths(package: host_mode.Package) -> set[str]:
    manifest = json.loads(package.files["/usr/lib/homelab/manifest.json"][0])
    return {entry.get("path") for entry in manifest["legacy"]}


def _wrapper_execs(package: host_mode.Package, tool: str) -> bool:
    body, mode, *_ = package.files[f"/usr/bin/{tool}"]
    return mode == 0o755 and f'exec /usr/lib/homelab/bin/{tool} "$@"' in body.decode()


def test_uv_reaches_both_hosts_and_daniel_box_keeps_the_old_copy(server, box):
    assert _wrapper_execs(server, "uv")
    assert _wrapper_execs(box, "uv")
    assert "/usr/local/bin/uv" not in _legacy_paths(box)
    assert "homelab-tools" in server.depends and "homelab-tools" in box.depends


def test_vale_reaches_daniel_server_alone_and_retires_the_old_copy(server, box):
    assert _wrapper_execs(server, "vale")
    assert "/usr/local/bin/vale" in _legacy_paths(server)
    assert "/usr/bin/vale" not in box.files
    assert "/usr/local/bin/vale" not in _legacy_paths(box)


@pytest.mark.parametrize(("subject", "file", "task"), GUARDED_TASKS)
def test_the_role_skips_the_subject_on_daniel_server_and_keeps_daniel_box(
    subject, file, task
):
    tasks = yaml_fast.safe_load((ROLE / "tasks" / file).read_text())
    when = next(t for t in tasks if t.get("name") == task)["when"]

    assert f"initial_setup_packaged_hosts['{subject}']" in str(when)
    assert packaged_guard_hosts([when], (HOST, "daniel-box"), ROLE) == {HOST}


def _task(name: str) -> dict:
    tasks = yaml_fast.safe_load((ROLE / "tasks" / "host-basics.yml").read_text())
    return next(t for t in tasks if t.get("name") == name)


def test_the_ansible_tools_keep_running_on_daniel_server():
    # ansible-core and ansible-lint stay until H7.63, and only prek leaves.
    install = _task("Install Python CLI tooling as uv tools")
    assert [item.partition("==")[0] for item in install["loop"]] == [
        "ansible-core",
        "ansible-lint",
    ]
    assert packaged_guard_hosts([install["when"]], (HOST,), ROLE) == frozenset()
    assert [
        item.partition("==")[0] for item in _task("Install prek as a uv tool")["loop"]
    ] == ["prek"]


@pytest.mark.parametrize(
    ("host", "uv"),
    [(HOST, "/usr/bin/uv"), ("daniel-box", "/home/op/.local/bin/uv")],
)
@pytest.mark.parametrize(
    "name",
    [
        "Install Python CLI tooling as uv tools",
        "Read the uv tools this host already has",
    ],
)
def test_the_tool_tasks_run_the_uv_their_host_is_sure_to_have(name, host, uv):
    # A rebuilt daniel-server has no ~/.local/bin/uv until the dotfiles repo has run.
    task = _task(name)
    rendered = jinja2.Template(task["vars"]["initial_setup_tool_uv"]).render(
        inventory_hostname=host,
        initial_setup_packaged_hosts=role_defaults(ROLE)[
            "initial_setup_packaged_hosts"
        ],
        initial_setup_user_home={"stdout": "/home/op"},
    )
    assert rendered.strip() == uv
    assert "{{ initial_setup_tool_uv }}" in task["ansible.builtin.command"]
