#!/usr/bin/env python3
"""The unattended session runs as its own user and holds nothing that can reach master.

The session reads third-party text: release notes, changelogs and PR bodies. So the unit runs
it as `renovate_agent_user` with a GitHub token that can neither push nor merge, and the one
privileged thing it may do is start `renovate-agent-land@<n>.service`, which re-checks the PR
and lands it as the operator. Each test pins one leg of that. A leg that slips fails no
deploy: the unit still starts, only with more reach than the design gives it.

Run: uv run pytest ansible/tests/setup/test_renovate_agent_identity.py
"""

import importlib.util
import re
import sys

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env

ROLE_NAME = "renovate_agent"
ROLE = ANSIBLE / "roles" / "setup" / ROLE_NAME
SERVICE_TASKS = ROLE / "tasks" / "service.yml"
UNIT = "renovate-agent.service.j2"
LANDER = "renovate-agent-land@.service.j2"
PROMPT = "prompt.txt.j2"
CONFIG_ENV = "config.env.j2"
HOOKS = ANSIBLE.parent / ".claude" / "hooks"

GH = "renovate_agent_gh_token"
CLAUDE = "renovate_agent_claude_oauth_token"
# Values no secret, path or login holds, so a render carrying one carries THAT variable.
HOME = "/srv/identity-sentinel-home"
CLONE = "/srv/identity-sentinel-clone"
USER = "identity-sentinel-user"
OPERATOR = "identity-sentinel-operator"
GH_SENTINEL = "identity-sentinel-gh-token"
CLAUDE_SENTINEL = "identity-sentinel-claude-token"
MOVED = {
    "renovate_agent_home": HOME,
    "renovate_agent_clone_dir": CLONE,
    "renovate_agent_user": USER,
    "sys_user": OPERATOR,
    GH: GH_SENTINEL,
    CLAUDE: CLAUDE_SENTINEL,
}
# Any PR number the lander accepts, standing in for the prompt's `<n>`.
PR = "3337"


def render(template: str, overrides: dict | None = None) -> str:
    return render_setup_text(ROLE_NAME, template, overrides)


def directive(unit_text: str, key: str) -> list[str]:
    """Every value assigned to `key`, with systemd's backslash continuations folded in."""
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


def task(prefix: str) -> dict:
    tasks = yaml_fast.safe_load(SERVICE_TASKS.read_text())
    found = [t for t in tasks if t.get("name", "").startswith(prefix)]
    assert len(found) == 1, f"expected one task named {prefix!r}, found {len(found)}"
    return found[0]


def test_the_session_runs_as_its_own_user_in_its_own_clone() -> None:
    unit = render(UNIT, MOVED)
    assert directive(unit, "User") == [USER]
    assert directive(unit, "WorkingDirectory") == [CLONE]
    envs = directive(unit, "Environment")
    assert f"HOME={HOME}" in envs
    assert any(e.startswith(f"PATH={HOME}/.local/bin:") for e in envs)
    assert directive(unit, "ExecStart")[0].startswith(f"/usr/bin/flock -n {HOME}/"), (
        "the run lock must sit in the agent's home: fs.protected_regular=2 refuses an O_CREAT "
        "open of the operator's old lock in sticky /run/lock"
    )
    config = render(CONFIG_ENV, MOVED)
    assert f"\nREPO_DIR={CLONE}\n" in config, (
        "the wrapper must cut its worktree in the agent's clone, never the primary checkout"
    )
    assert f"\nCLAUDE_BIN={HOME}/.local/bin/claude\n" in config


# The directives that decide what the session's processes run and read.
_REACHING = ("User", "WorkingDirectory", "Environment", "EnvironmentFile", "ExecStart")


def operator_reach(unit_text: str, operator: str) -> list[str]:
    """Every directive value naming the operator's user or home. Empty means none does.

    `/usr/local/bin/uv` counts: it is a symlink into the operator's home, which ProtectHome=
    hides from this unit.
    """
    return [
        f"{key}={value}"
        for key in _REACHING
        for value in directive(unit_text, key)
        if value == operator or "/home/" in value or "/usr/local/bin/uv" in value
    ]


def test_the_session_unit_reaches_nothing_of_the_operators() -> None:
    assert operator_reach(render(UNIT, {"sys_user": OPERATOR}), OPERATOR) == []


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
def test_the_unit_hides_what_another_local_user_could_still_read(
    key: str, value: str
) -> None:
    assert directive(render(UNIT), key) == [value]


def test_the_tokens_reach_the_session_only_through_session_env() -> None:
    session = render("session.env.j2", MOVED)
    assert f"\nGH_TOKEN={GH_SENTINEL}\n" in session
    assert f"\nCLAUDE_CODE_OAUTH_TOKEN={CLAUDE_SENTINEL}\n" in session
    for template in (UNIT, LANDER, CONFIG_ENV, PROMPT):
        text = render(template, MOVED)
        assert GH_SENTINEL not in text and CLAUDE_SENTINEL not in text, (
            f"{template} carries a session token"
        )
    assert directive(render(UNIT), "EnvironmentFile") == [
        "/etc/renovate-agent/session.env"
    ]
    written = task("Write the session's GitHub token")
    tpl = written["ansible.builtin.template"]
    assert (tpl["dest"], tpl["owner"], tpl["mode"]) == (
        "/etc/renovate-agent/session.env",
        "root",
        "0600",
    )
    assert written["no_log"] is True


def test_the_session_cannot_rewrite_its_own_config() -> None:
    """A steered session could raise its budget or swap PROMPT_FILE for the next run."""
    tpl = task("Write agent config")["ansible.builtin.template"]
    assert (tpl["owner"], tpl["group"], tpl["mode"]) == (
        "root",
        "{{ renovate_agent_user }}",
        "0640",
    )


def arming_refused(secrets: dict) -> bool:
    """Whether the role's arming assert fails with these secrets set."""
    guard = task("Refuse to arm the agent")
    assert guard["when"] == "renovate_agent_enabled", "the assert must gate arming only"
    env = make_ansible_env()
    return not all(
        env.compile_expression(that)(**secrets)
        for that in guard["ansible.builtin.assert"]["that"]
    )


def test_arming_with_both_credentials_passes() -> None:
    assert not arming_refused({GH: "a", CLAUDE: "b"})


@pytest.mark.parametrize("missing", [GH, CLAUDE])
@pytest.mark.parametrize("how", ["unset", "empty"])
def test_arming_without_either_credential_is_refused(missing: str, how: str) -> None:
    secrets = {GH: "a", CLAUDE: "b"}
    if how == "unset":
        del secrets[missing]
    else:
        secrets[missing] = ""
    assert arming_refused(secrets)


def _seconds(span: str) -> int:
    minutes = re.fullmatch(r"(\d+)min", span)
    assert minutes, f"expected a span in minutes, got {span!r}"
    return int(minutes.group(1)) * 60


def test_the_sessions_bash_ceiling_and_run_timeout_outlast_one_landing() -> None:
    """The session blocks in `systemctl start` for the whole landing."""
    landing_s = _seconds(directive(render(LANDER), "TimeoutStartSec")[-1])
    ceilings = [
        e.split("=", 1)[1]
        for e in directive(render(UNIT), "Environment")
        if e.startswith("BASH_MAX_TIMEOUT_MS=")
    ]
    assert ceilings and int(ceilings[-1]) > landing_s * 1000
    assert f"Bash timeout of {ceilings[-1]} ms" in " ".join(render(PROMPT).split()), (
        "the prompt must quote the ceiling the unit sets"
    )
    run_timeout = int(render(CONFIG_ENV).split("\nRUN_TIMEOUT_S=")[1].split("\n")[0])
    assert run_timeout > landing_s


def prompt_commands() -> list[str]:
    """The prompt's backticked `systemctl` commands, `<n>` filled in."""
    prompt = " ".join(render(PROMPT).split())
    return [c.replace("<n>", PR) for c in re.findall(r"`(systemctl [^`]+)`", prompt)]


def test_the_prompt_lands_through_the_lander_the_polkit_rule_admits() -> None:
    commands = prompt_commands()
    unit = f"renovate-agent-land@{PR}.service"
    assert f"systemctl start {unit}" in commands, commands
    assert (ROLE / "templates" / LANDER).is_file()
    rule = render("50-renovate-agent-land.rules.j2")
    pattern = re.search(r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rule)
    assert pattern and re.fullmatch(pattern.group(1).strip("^$"), unit)
    lander = (ROLE / "files" / "land_renovate_pr.py").read_text()
    results = re.search(r'^RESULTS_DIR = "([^"]+)"', lander, re.M)
    assert results and f"`{results.group(1)}/<n>.verdict`" in render(PROMPT)
    assert "findings.py open" not in render(PROMPT), (
        "the token has no issues write, so a prompt asking for a finding fails every time"
    )


def test_the_hooks_run_in_the_worktree_the_wrapper_builds() -> None:
    """The shim's defaults are the operator's checkout and uv, which this user cannot read, and
    a guard that cannot start asks — an unattended denial on every Bash call."""
    envs = dict(e.split("=", 1) for e in directive(render(UNIT, MOVED), "Environment"))
    config = dict(
        line.split("=", 1)
        for line in render(CONFIG_ENV, MOVED).splitlines()
        if "=" in line and not line.startswith("#")
    )
    # renovate_agent.main builds the run worktree's path the same way.
    built = f"{config['REPO_DIR']}/.claude/worktrees/{config['WORKTREE']}"
    assert envs.get("RUN_HOOK_PROJECT_DIR") == built
    assert envs.get("RUN_HOOK_UV") == f"{HOME}/.local/bin/uv"
    shim = (HOOKS / "run-hook.sh").read_text()
    assert "${RUN_HOOK_PROJECT_DIR:-" in shim and "${RUN_HOOK_UV:-" in shim, (
        "the hook shim no longer reads the overrides this unit sets"
    )


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
        "cwd": str(ANSIBLE.parent),
    }
    verdicts = [
        verdict
        for filename, module in dispatcher._DECISION_ARMS
        if (verdict := dispatcher.load_arm(filename, module).decision(payload))
    ]
    return dispatcher.merge(verdicts)[0]


def test_the_project_guard_lets_the_session_land_and_read_the_verdict() -> None:
    """An unattended `ask` is a denial, so a guard rule that catches these ends every landing."""
    commands = prompt_commands() + [f"cat /var/lib/renovate-agent-land/{PR}.verdict"]
    assert len(commands) == 3, commands
    assert {c: guard_decision(c) for c in commands} == dict.fromkeys(commands)


def test_the_project_guard_still_denies_what_it_should() -> None:
    assert guard_decision("gh issue create --title x --body y") == "deny"
