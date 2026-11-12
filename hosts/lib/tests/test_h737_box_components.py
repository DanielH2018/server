"""daniel-box's package carries Renovate, fake-remux and the GitHub checks (H7.37).

The scripts' behaviour has its own tests, under each component's `tests/` and in
`ansible/tests/setup/`. These hold what the move added: the units and timer slots the package
ships, the secret outputs, the Sonarr pin the package renders literally, the legacy entries, and
the agent's setup script run against stubs.

Run: uv run pytest hosts/lib/tests/test_h737_box_components.py
"""

import re

import pytest
import yaml
from _setup_render import render_component_text

from lib.proc_testing import fake_bin, path_with, run, write_exec
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

UNITS = "/usr/lib/systemd/system"
COMPONENTS = REPO / "hosts" / "components"


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-box")


def text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def legacy(component: str) -> list[dict]:
    spec = yaml.safe_load((COMPONENTS / component / "component.yml").read_text())
    return spec["legacy"]


@pytest.mark.parametrize(
    ("timer", "calendar"),
    [
        ("homelab-renovate-notify", "*-*-* 13:00:00"),
        ("homelab-renovate-agent", "*-*-* 06:00:00 America/Chicago"),
        ("homelab-fake-remux-scan", "*-*-* 04:45:00"),
        ("homelab-fake-remux-reconcile", "*:0/20:00"),
        ("homelab-mkv-attachment-repair", "*:0/15:00"),
        ("homelab-fake-remux-health", "*:0/10:00"),
        ("homelab-github-ruleset-drift", "*-*-* 06:40:00"),
        ("homelab-github-interaction-limit", "*-*-* 06:50:00"),
    ],
)
def test_each_timer_is_enabled_at_the_slot_the_ansible_unit_or_cron_held(
    package, timer, calendar
):
    assert f"OnCalendar={calendar}\n" in text(package, f"{UNITS}/{timer}.timer")
    assert f"{timer}.timer" in package.enable_links()


@pytest.mark.parametrize(
    ("service", "identifier", "script"),
    [
        ("homelab-fake-remux-scan", "fake-remux-scan", "fake_remux.py scan"),
        (
            "homelab-fake-remux-reconcile",
            "fake-remux-reconcile",
            "fake_remux.py replace",
        ),
        (
            "homelab-mkv-attachment-repair",
            "mkv-attachment-repair",
            "mkv_attachment_repair.py",
        ),
    ],
)
def test_the_media_services_share_one_lock_and_keep_their_journal_tag(
    package, service, identifier, script
):
    unit = text(package, f"{UNITS}/{service}.service")
    folded = re.sub(r"\\\n\s*", " ", unit)
    assert (
        "ExecStart=/usr/bin/flock -w 600 /var/lock/autofix-fake-remux.lock " in folded
    )
    assert folded.rstrip().endswith(f"/usr/lib/homelab/fake-remux/{script}")
    assert f"SyslogIdentifier={identifier}\n" in unit
    assert "StateDirectory=autofix-fake-remux\n" in unit
    assert "AssertPathExists=/etc/autofix-fake-remux/config.env\n" in unit
    assert package.policies[f"{service}.service"] == "manual"


def test_the_fake_remux_config_keeps_the_path_the_scripts_fall_back_to():
    """A wrong path reads as `disabled (no Sonarr API key)` and exits 0, scanning nothing."""
    spec = yaml.safe_load((COMPONENTS / "fake-remux/component.yml").read_text())
    [output] = spec["outputs"]
    assert output["path"] == "/etc/autofix-fake-remux/config.env"
    assert sorted(output["keys"]) == ["arr_discord_webhook_url", "sonarr_api_key"]
    for module in ("fake_remux_scan.py", "fake_remux_replace.py"):
        source = (COMPONENTS / "fake-remux/files/fake_remux_lib" / module).read_text()
        assert output["path"] in source


def test_the_sonarr_pin_in_the_package_equals_the_services():
    """The package cannot read the cluster, so a changed pin strands the reconciler."""
    package_copy = yaml.safe_load(
        (COMPONENTS / "fake-remux/defaults/main.yml").read_text()
    )["fake_remux_sonarr_cluster_ip"]
    sonarr = REPO / "ansible/roles/k8s/sonarr"
    cluster_copy = yaml.safe_load((sonarr / "defaults/main.yml").read_text())
    assert package_copy == cluster_copy["sonarr_k8s_cluster_ip"]
    template = (sonarr / "templates/service.yaml.j2").read_text()
    assert "clusterIP: {{ sonarr_k8s_cluster_ip }}\n" in template


def test_the_package_needs_the_media_tools(package):
    assert {"ffmpeg", "mkvtoolnix"} <= set(package.depends)


def test_the_agent_user_sits_in_the_homelab_uid_band(package):
    [user] = [u for u in package.users if u["name"] == "renovate-agent"]
    assert (user["uid"], user["home"]) == (901, "/var/lib/renovate-agent")


@pytest.mark.parametrize(
    ("component", "entries"),
    [
        (
            "renovate-notify",
            [
                {"unit": "renovate-notify.timer"},
                {"unit": "renovate-notify-alert.service"},
            ],
        ),
        (
            "renovate-agent",
            [
                {"unit": "renovate-agent.timer"},
                {"path": "/etc/renovate-agent/alert-webhook.env"},
            ],
        ),
        (
            "fake-remux",
            [
                {"cron_file": name}
                for name in (
                    "fake-remux-scan",
                    "fake-remux-reconcile",
                    "mkv-attachment-repair",
                    "fake-remux-health",
                )
            ],
        ),
        (
            "github-checks",
            [
                {"unit": "kuma-check-github-ruleset-drift.timer"},
                {"unit": "kuma-check-github-interaction-limit.timer"},
            ],
        ),
    ],
)
def test_each_component_retires_what_ansible_installed(component, entries):
    assert [e for e in entries if e not in legacy(component)] == []


def _setup_script(tmp_path, stubs):
    """The rendered setup script, pointed at `tmp_path` and at `stubs` for the agent's PATH."""
    home, operator = tmp_path / "home", tmp_path / "operator"
    body = render_component_text(
        "renovate-agent",
        "renovate-agent-setup.sh.j2",
        {
            "renovate_agent_home": str(home),
            "renovate_agent_clone_dir": str(home / "server"),
        },
    )
    body = body.replace("operator=/home/ubuntu", f"operator={operator}")
    body = body.replace("PATH=/usr/bin:/bin", f"PATH={stubs}:/usr/bin:/bin")
    home.mkdir()
    return write_exec(tmp_path / "setup.sh", body), home, operator


@pytest.fixture
def setup(tmp_path):
    calls = tmp_path / "calls"
    stubs = fake_bin(
        tmp_path / "bin",
        runuser=f'user=$2\nshift 3\necho "$user $*" >> {calls}.runuser\nexec "$@"\n',
        install="true\n",
        git=f'echo "git $*" >> {calls}\n',
        curl=f'echo "curl $*" >> {calls}\nwhile [ "$1" != -o ]; do shift; done\n'
        f'printf \'mkdir -p "$HOME/.local/bin"; touch "$HOME/.local/bin/claude"; chmod +x "$HOME/.local/bin/claude"\' > "$2"\n',
    )
    script, home, operator = _setup_script(tmp_path, stubs)
    write_exec(operator / ".local/bin/uv", f'echo "uv $*" >> {calls}\n')
    write_exec(operator / ".local/bin/uvx", "true\n")
    (operator / ".local/share/uv/tools/prek/bin").mkdir(parents=True)
    write_exec(operator / ".local/share/uv/tools/prek/bin/prek", "true\n")
    guard = operator / ".local/share/claude-guard/claude_guard"
    guard.mkdir(parents=True)
    (guard / "rules.py").write_text("x = 1\n")
    worktree = operator / ".local/share/claude-worktree"
    worktree.mkdir(parents=True)
    (worktree / "claude_worktree.py").write_text("y = 1\n")
    env = {"PATH": path_with(stubs)}
    return script, home, calls, env


def test_the_setup_gives_the_agent_its_tools_python_claude_and_clone(setup):
    script, home, calls, env = setup
    run(["bash", str(script)], env=env, check=True)
    for rel in (
        ".local/bin/uv",
        ".local/bin/uvx",
        ".local/bin/prek",
        ".local/bin/claude",
        ".local/share/claude-guard/claude_guard/rules.py",
        ".local/share/claude-worktree/claude_worktree.py",
    ):
        assert (home / rel).exists(), rel
    log = calls.read_text()
    assert re.search(r"^uv python install \d+\.\d+\.\d+$", log, re.M)
    assert "curl -fsSL --retry 3 -o" in log
    assert (
        f"git clone --quiet https://github.com/DanielH2018/server.git {home}/server"
        in log
    )


def test_the_agent_runs_every_command_that_writes_into_its_home(setup):
    """A session can replace any path in its home with a link, so root must not write there."""
    script, home, calls, env = setup
    run(["bash", str(script)], env=env, check=True)
    log = calls.with_name("calls.runuser").read_text()
    writes = [
        *(
            rf"cp -RT --remove-destination \S+ {home}/{rel}"
            for rel in (
                ".local/bin/uv",
                ".local/bin/uvx",
                ".local/bin/prek",
                ".local/share/claude-guard/claude_guard",
                ".local/share/claude-worktree/claude_worktree.py",
            )
        ),
        rf"mkdir -p {home}/\.local/bin .*",
        rf"{home}/\.local/bin/uv python install \S+",
        r"env CLAUDE_INSTALL_ALLOW_SUDO=1 bash \S+",
        rf"git clone --quiet \S+ {home}/server",
    ]
    for write in writes:
        assert re.search(rf"^renovate-agent env .*{write}$", log, re.M), write


def test_a_second_setup_reinstalls_nothing_it_can_skip(setup):
    script, home, calls, env = setup
    run(["bash", str(script)], env=env, check=True)
    (home / "server/.git").mkdir(parents=True)
    calls.write_text("")
    run(["bash", str(script)], env=env, check=True)
    log = calls.read_text()
    assert "curl" not in log and "git clone" not in log
    assert "uv python install" in log, "a host_python_version bump must still converge"
