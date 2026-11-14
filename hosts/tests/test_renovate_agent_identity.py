#!/usr/bin/env python3
"""The unattended session runs as its own user and holds nothing that can reach master.

The session reads third-party text: release notes, changelogs and PR bodies. So the unit runs
it as `renovate-agent` with a GitHub token that can neither push nor merge, and the one
privileged thing it can do is write a request that the controller, `renovate-agent-ctl`, may
act on. The lander re-checks the PR and lands it as the operator. Each test pins one leg of
that. A leg that slips fails no deploy: the unit still starts, only with more reach than the
design gives it.

Run: uv run pytest hosts/tests/test_renovate_agent_identity.py
"""

import importlib.util
import json
import re
import shlex
import sys

import pytest
import run_requests
from _helpers import REPO
from _setup_render import COMPONENTS, render_component_text
from lib import yaml_fast
from lib.proc_testing import run

NAME = "renovate-agent"
COMPONENT = COMPONENTS / NAME
SPEC = yaml_fast.safe_load((COMPONENT / "component.yml").read_text())
UNIT = "homelab-renovate-agent.service.j2"
LANDER = "renovate-agent-land@.service.j2"
PROMPT = "prompt.txt.j2"
CONFIG_ENV = "config.env.j2"
SESSION_UNIT = "homelab-renovate-agent-session.service.j2"
SESSION_CONF = "session.conf.j2"
# The pinned users (templates/renovate-agent-pins.j2), which no inventory value moves.
SESSION_USER = "renovate-agent"
CTL_USER = "renovate-agent-ctl"
HOOKS = REPO / ".claude" / "hooks"

GH = "renovate_agent_gh_token"
GH_READ = "renovate_agent_gh_read_token"
CLAUDE = "renovate_agent_claude_oauth_token"
# Values no secret, path or login holds, so a render carrying one carries THAT variable.
HOME = "/srv/identity-sentinel-home"
CLONE = "/srv/identity-sentinel-clone"
USER = "identity-sentinel-user"
OPERATOR = "identity-sentinel-operator"
GH_SENTINEL = "identity-sentinel-gh-token"
GH_READ_SENTINEL = "identity-sentinel-gh-read-token"
WEBHOOK_SENTINEL = "identity-sentinel-webhook"
CLAUDE_SENTINEL = "identity-sentinel-claude-token"
MOVED = {
    "renovate_agent_home": HOME,
    "renovate_agent_clone_dir": CLONE,
    "renovate_agent_user": USER,
    "sys_user": OPERATOR,
    GH: GH_SENTINEL,
    GH_READ: GH_READ_SENTINEL,
    CLAUDE: CLAUDE_SENTINEL,
    "discord_alert_webhook": WEBHOOK_SENTINEL,
    "renovate_agent_ctl_home": "/srv/identity-sentinel-ctl",
}
# Any PR number the lander accepts, standing in for the prompt's `<n>`.
PR = "3337"


class _Starts:
    """Records each process the runner starts and answers it as a lander that settled."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None, timeout=120, stdin_text=None):
        self.calls.append(argv)
        return 0, "success\n"


def render(template: str, overrides: dict | None = None) -> str:
    return render_component_text(NAME, template, overrides)


def directive(unit_text: str, key: str) -> list[str]:
    """Every value assigned to `key`, with systemd's backslash continuations folded in."""
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def output(path: str) -> dict:
    found = [o for o in SPEC["outputs"] if o["path"] == path]
    assert len(found) == 1, f"expected one output at {path!r}, found {len(found)}"
    return found[0]


def test_the_session_runs_as_its_own_user_in_its_own_clone() -> None:
    """The user is pinned, so the inventory's renovate_agent_user does not move it."""
    unit = render(SESSION_UNIT, MOVED)
    assert directive(unit, "User") == [SESSION_USER]
    assert directive(unit, "WorkingDirectory") == [f"-{CLONE}"]
    envs = directive(unit, "Environment")
    assert f"HOME={HOME}" in envs
    assert any(e.startswith(f"PATH={HOME}/.local/bin:") for e in envs)
    config = render(SESSION_CONF, MOVED)
    assert f"\nREPO_DIR={CLONE}\n" in config, (
        "the session must cut its worktree in the agent's clone, never the primary checkout"
    )
    assert f"\nCLAUDE_BIN={HOME}/.local/bin/claude\n" in config


def test_the_runner_runs_as_the_controller_with_its_own_lock() -> None:
    """dotfiles#967's I1: the controller runs the runner; the session's group lets it read
    what the session writes into the exchange."""
    unit = render(UNIT, MOVED)
    assert directive(unit, "User") == [CTL_USER]
    assert directive(unit, "SupplementaryGroups") == [SESSION_USER]
    assert directive(unit, "ExecStart")[0].startswith(
        "/usr/bin/flock -n /var/lib/renovate-agent-ctl/run.lock"
    ), (
        "fs.protected_regular=2 refuses an O_CREAT open of another user's lock in /run/lock"
    )
    assert not any(
        HOME in v for v in directive(unit, "ExecStart") + directive(unit, "Environment")
    ), "the controller must run nothing from the session user's home"


# The directives that decide what the session's processes run and read.
_REACHING = ("User", "WorkingDirectory", "Environment", "EnvironmentFile", "ExecStart")


def operator_reach(unit_text: str, operator: str) -> list[str]:
    """Every directive value naming the operator's user or home. Empty means none does.

    `/usr/local/bin/uv` counts: it is a copy of the operator's uv install. The agent runs its own
    copy and Python under its own home.
    """
    return [
        f"{key}={value}"
        for key in _REACHING
        for value in directive(unit_text, key)
        if value == operator or "/home/" in value or "/usr/local/bin/uv" in value
    ]


@pytest.mark.parametrize("template", [UNIT, SESSION_UNIT])
def test_the_session_unit_reaches_nothing_of_the_operators(template) -> None:
    assert operator_reach(render(template, {"sys_user": OPERATOR}), OPERATOR) == []


def test_a_unit_running_from_the_operators_home_is_flagged() -> None:
    unit = "User=op\nEnvironment=PATH=/home/op/.local/bin\nExecStart=/usr/local/bin/uv run x\n"
    assert operator_reach(unit, "op") == [
        "User=op",
        "Environment=PATH=/home/op/.local/bin",
        "ExecStart=/usr/local/bin/uv run x",
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NoNewPrivileges", "yes"),
        ("ProtectHome", "yes"),
        ("ProtectProc", "invisible"),
        ("PrivateTmp", "yes"),
    ],
)
@pytest.mark.parametrize("template", [UNIT, SESSION_UNIT])
def test_the_unit_hides_what_another_local_user_could_still_read(
    key: str, value: str, template: str
) -> None:
    assert directive(render(template), key) == [value]


def test_the_session_holds_only_the_read_token_and_the_controller_the_write_one() -> (
    None
):
    """dotfiles#967's I1: the write token never reaches the session's environment."""
    session = render("session.env.j2", MOVED)
    assert f"\nGH_TOKEN={GH_READ_SENTINEL}\n" in session
    assert f"\nCLAUDE_CODE_OAUTH_TOKEN={CLAUDE_SENTINEL}\n" in session
    assert GH_SENTINEL + "\n" not in session
    ctl = render("ctl.env.j2", MOVED)
    assert f"\nGH_TOKEN={GH_SENTINEL}\n" in ctl
    assert GH_READ_SENTINEL not in ctl and CLAUDE_SENTINEL not in ctl
    for template in (UNIT, SESSION_UNIT, LANDER, CONFIG_ENV, SESSION_CONF, PROMPT):
        text = render(template, MOVED)
        assert not any(
            s in text for s in (GH_SENTINEL, GH_READ_SENTINEL, CLAUDE_SENTINEL)
        ), f"{template} carries a token"
    assert directive(render(SESSION_UNIT), "EnvironmentFile") == [
        "/etc/renovate-agent/session.env"
    ]
    assert directive(render(UNIT), "EnvironmentFile") == ["/etc/renovate-agent/ctl.env"]
    for path, keys in (
        ("/etc/renovate-agent/session.env", [GH_READ, CLAUDE]),
        ("/etc/renovate-agent/ctl.env", [GH]),
    ):
        written = output(path)
        assert (written["owner"], written["group"], written["mode"]) == (
            "root",
            "root",
            "0600",
        )
        assert sorted(written["keys"]) == sorted(keys)


def test_the_session_cannot_rewrite_its_own_config() -> None:
    """A steered session could raise its budget or swap PROMPT_FILE for the next run."""
    config = output("/etc/renovate-agent/config.env")
    assert (config["owner"], config["group"], config["mode"]) == (
        "root",
        CTL_USER,
        "0640",
    )
    # The session reads only its own config, which carries no webhook.
    assert WEBHOOK_SENTINEL not in render(SESSION_CONF, MOVED)
    assert WEBHOOK_SENTINEL in render(CONFIG_ENV, MOVED)


def _seconds(span: str) -> int:
    minutes = re.fullmatch(r"(\d+)min", span)
    assert minutes, f"expected a span in minutes, got {span!r}"
    return int(minutes.group(1)) * 60


def _config(key: str) -> int:
    return int(render(CONFIG_ENV).split(f"\n{key}=")[1].split("\n")[0])


def test_the_runs_deadline_holds_the_session_and_one_whole_landing() -> None:
    """The runner lands after the session, inside RUN_TIMEOUT_S."""
    landing_s = _seconds(directive(render(LANDER), "TimeoutStartSec")[-1])
    assert _config("SESSION_TIMEOUT_S") + landing_s <= _config("RUN_TIMEOUT_S")


def prompt_commands() -> list[str]:
    """The prompt's backticked `systemctl` commands, `<n>` filled in."""
    prompt = " ".join(render(PROMPT).split())
    return [c.replace("<n>", PR) for c in re.findall(r"`(systemctl [^`]+)`", prompt)]


def test_the_runner_lands_through_the_lander_the_polkit_rule_admits() -> None:
    """The runner starts the unit the rule admits and reads the file the lander writes."""
    # fact: hosts/components/renovate-agent/CLAUDE.md#Autonomous-role contract (it merges and deploys with no human in the loop)
    gh = _Starts()
    run_requests._land(int(PR), run_requests.AgentTools(run=gh), 60)
    unit = f"renovate-agent-land@{PR}.service"
    assert ["systemctl", "start", unit] in gh.calls
    rule = render("50-renovate-agent-land.rules.j2")
    pattern = re.search(r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rule)
    assert pattern and re.fullmatch(pattern.group(1).strip("^$"), unit)
    lander = (COMPONENT / "files" / "land_renovate_pr.py").read_text()
    results = re.search(r'^RESULTS_DIR = "([^"]+)"', lander, re.M)
    assert results and results.group(1) == run_requests.VERDICT_DIR


def test_the_session_is_told_to_nominate_and_never_to_land() -> None:
    assert prompt_commands() == []
    assert '"action": "land"' in render(PROMPT)
    assert "findings.py open" not in render(PROMPT), (
        "the token has no issues write, so a prompt asking for a finding fails every time"
    )


def test_the_hooks_run_in_the_worktree_the_wrapper_builds() -> None:
    """The shim's defaults are the operator's checkout and uv, which this user cannot read, and
    a guard that cannot start asks — an unattended denial on every Bash call."""
    session = render("homelab-renovate-agent-session.service.j2", MOVED)
    envs = dict(e.split("=", 1) for e in directive(session, "Environment"))
    config = dict(
        line.split("=", 1)
        for line in render(CONFIG_ENV, MOVED).splitlines()
        if "=" in line and not line.startswith("#")
    )
    # renovate_session.main builds the run worktree's path the same way.
    built = f"{config['REPO_DIR']}/.claude/worktrees/{config['WORKTREE']}"
    assert envs.get("RUN_HOOK_PROJECT_DIR") == built
    assert envs.get("RUN_HOOK_UV") == f"{HOME}/.local/bin/uv"
    shim = (HOOKS / "run-hook.sh").read_text()
    assert "${RUN_HOOK_PROJECT_DIR:-" in shim and "${RUN_HOOK_UV:-" in shim, (
        "the hook shim no longer reads the overrides this unit sets"
    )


def test_the_agent_gets_claude_guard_where_the_hook_looks_for_it() -> None:
    """Without the package block-footguns asks on every `gh` command, which is the session's
    whole job, and an unattended `ask` is a denial."""
    lookup = re.search(
        r'^_CLAUDE_GUARD_DIR = Path\("~(/[^"]+)"\)',
        (HOOKS / "_claude_guard.py").read_text(),
        re.M,
    )
    assert lookup, "the hook no longer names its claude_guard directory"
    rel = lookup.group(1)
    setup = render("renovate-agent-setup.sh.j2", MOVED)
    assert (
        f'copy_from_operator "$operator{rel}/claude_guard" "$home{rel}/claude_guard"'
        in setup
    )


SETTINGS_SRC = "files/claude-settings.json"


def session_settings() -> dict:
    return json.loads((COMPONENT / SETTINGS_SRC).read_text())


def hook_commands(settings: dict, event: str, matcher: str) -> list[str]:
    return [
        hook["command"]
        for entry in settings.get("hooks", {}).get(event, [])
        if entry.get("matcher") == matcher
        for hook in entry["hooks"]
    ]


def test_the_session_settings_run_the_shared_guard_hooks() -> None:
    """claude-guard on every Bash call, and the secret-file deny on every file tool."""
    settings = session_settings()
    assert hook_commands(settings, "PreToolUse", "Bash") == [
        "~/.claude/hooks/guard-pre-tool-use.sh"
    ]
    assert hook_commands(settings, "PermissionRequest", "Bash") == [
        "~/.claude/hooks/guard-permission-request.sh"
    ]
    assert hook_commands(settings, "PreToolUse", "Read|Edit|Write|NotebookEdit") == [
        "~/.claude/hooks/protect-secrets.sh"
    ]


@pytest.mark.parametrize(
    "rule",
    [
        "Bash(sudo:*)",
        "Bash(su:*)",
        "Bash(kubectl delete:*)",
        "Bash(git push --force)",
        "Read(~/.config/gh/**)",
        "Read(//run/homelab-secrets/**)",
        "Edit(~/.claude/settings.json)",
        "Edit(~/.claude/hooks/**)",
        "Edit(~/.local/share/claude-guard/**)",
    ],
)
def test_the_session_settings_deny_the_floor(rule: str) -> None:
    assert rule in session_settings()["permissions"]["deny"]


def test_the_session_settings_lock_out_bypass_mode() -> None:
    permissions = session_settings()["permissions"]
    assert permissions["disableBypassPermissionsMode"] == "disable"
    assert "allow" not in permissions, (
        "the floor ships denies only; the wrapper's permission mode decides the rest"
    )


def test_setup_installs_the_settings_at_the_path_the_guardrail_audit_watches() -> None:
    shipped = [f for f in SPEC["files"] if f.get("src") == SETTINGS_SRC]
    assert len(shipped) == 1
    setup = render("renovate-agent-setup.sh.j2", MOVED)
    assert f"\nsettings={shipped[0]['dst']}\n" in setup
    assert (
        'as_agent cp --remove-destination "$settings" "$home/.claude/settings.json"'
        in setup
    ), "the agent, not root, must write into its own home"
    assert 'copy_from_operator "$operator/.claude/hooks" "$home/.claude/hooks"' in setup


def test_the_session_settings_carry_the_auto_mode_deny_floor() -> None:
    """The wrapper runs the session in auto mode, where the classifier reads these rules."""
    auto = session_settings()["autoMode"]
    assert auto["classifyAllShell"] is True
    assert any("`master`" in rule for rule in auto["hard_deny"])
    assert auto["soft_deny"] and auto["environment"]
    assert "allow" not in auto


def test_the_guardrail_ack_explains_the_copies_setup_makes() -> None:
    """A rollout of the settings or the operator's hooks must not post as unexplained."""
    ack = json.loads(
        render_component_text("claude-code", "claude-guardrail-ack.json.j2")
    )
    [shipped] = [f["dst"] for f in SPEC["files"] if f.get("src") == SETTINGS_SRC]
    home = "/var/lib/renovate-agent"
    assert {c["dst"]: c["src"] for c in ack["copies"]} == {
        f"{home}/.claude/settings.json": shipped,
        f"{home}/.claude/hooks": "/home/ubuntu/.claude/hooks",
    }
    assert {c["dst"] for c in ack["copies"]} <= set(ack["paths"])


def guard_decision(command: str) -> str | None:
    """The decision the repo's PreToolUse:Bash dispatcher reaches for `command`, or None."""
    sys.path.insert(0, str(HOOKS))  # the arms import _hook_common
    spec = importlib.util.spec_from_file_location(
        "bash_pretool", HOOKS / "bash-pretool.py"
    )
    assert spec and spec.loader
    dispatcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatcher)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(REPO),
    }
    verdicts = [
        verdict
        for filename, module in dispatcher._DECISION_ARMS
        if (verdict := dispatcher.load_arm(filename, module).decision(payload))
    ]
    return dispatcher.merge(verdicts)[0]


def test_the_project_guard_still_denies_what_it_should() -> None:
    assert guard_decision("gh issue create --title x --body y") == "deny"


def test_land_sh_ends_its_landing_before_the_lander_script_kills_it() -> None:
    """land_renovate_pr.py's LAND_TIMEOUT_S counts from land.sh's start, as LAND_DEADLINE does.

    The reboot window can add LAND_WINDOW_GRACE to the promotion wait (#5457), so the script's
    kill covers both.
    """
    script = (COMPONENT / "files" / "land_renovate_pr.py").read_text()
    (timeout,) = re.findall(r"^LAND_TIMEOUT_S = (\d+)$", script, re.MULTILINE)
    deadlines = [
        e.split("=", 1)[1]
        for e in directive(render(LANDER), "Environment")
        if e.startswith("LAND_DEADLINE=")
    ]
    graces = [
        e.split("=", 1)[1]
        for e in directive(render(LANDER), "Environment")
        if e.startswith("LAND_WINDOW_GRACE=")
    ]
    assert deadlines and graces
    assert 0 < int(deadlines[-1]) + int(graces[-1]) <= int(timeout)


# What the unit spends before land.sh starts its clock: `uv run` startup, the denylist read and
# `gh pr view`, plus the script writing its verdict after land.sh returns.
SCRIPT_OVERHEAD_S = 60


def test_the_lock_wait_and_the_landing_both_fit_inside_the_units_timeout() -> None:
    """systemd counts the flock wait against TimeoutStartSec; LAND_DEADLINE does not (#5203)."""
    unit = render(LANDER)
    timeout = _seconds(directive(unit, "TimeoutStartSec")[-1])
    (flock_wait,) = re.findall(r"flock -w (\d+) ", directive(unit, "ExecStart")[0])
    (deadline,) = [
        e.split("=", 1)[1]
        for e in directive(unit, "Environment")
        if e.startswith("LAND_DEADLINE=")
    ]
    script = (COMPONENT / "files" / "land_renovate_pr.py").read_text()
    (land_timeout,) = re.findall(r"^LAND_TIMEOUT_S = (\d+)$", script, re.MULTILINE)
    landing = max(int(deadline), int(land_timeout))
    assert int(flock_wait) + landing + SCRIPT_OVERHEAD_S <= timeout


def test_a_lock_wait_that_times_out_leaves_no_earlier_verdict(tmp_path) -> None:
    """The verdict file is overwritten before the flock wait, not only once the script runs."""
    unit = render(LANDER)
    script = (COMPONENT / "files" / "land_renovate_pr.py").read_text()
    (results_dir,) = re.findall(r'^RESULTS_DIR = "([^"]+)"$', script, re.MULTILINE)
    (pre,) = directive(unit, "ExecStartPre")
    stale = tmp_path / f"{PR}.verdict"
    stale.write_text("VERDICT: settled\n")
    command = shlex.split(pre.replace("%i", PR).replace(results_dir, str(tmp_path)))
    run(command, check=True)
    assert stale.read_text().startswith("VERDICT: none")


def test_the_landers_placeholder_stops_the_run() -> None:
    """A lander systemd stops mid-landing leaves its placeholder, which is no verdict (#5205)."""
    script = (COMPONENT / "files" / "land_renovate_pr.py").read_text()
    (pending,) = re.findall(r'atomic_write\(result, "([A-Z]+)\\n"\)', script)
    landed, go_on, _ = run_requests.grade("exit-code", pending + "\n")
    assert not landed and not go_on
