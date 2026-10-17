#!/usr/bin/env python3
"""The agent's unattended triage unit runs capped, as the agent, and only the agent may start it.

`roles/setup/claude_code/tasks/triage.yml` installs claude-triage@.service, its polkit rule and
its two pages. Each test pins one leg of the design in docs/claude-agent-user.md,
"Alert-triggered triage". The runner's own behaviour is tested beside it, in
roles/setup/claude_code/tests/test_claude_triage_run.py.

Run: uv run pytest ansible/tests/setup/test_claude_triage.py
"""

import ast
import re

import pytest
from _helpers import ANSIBLE
from _k8s_render import render_role_template
from _setup_render import render_setup_text
from deploy_tools.land_lib import policy
from lib import yaml_fast

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
UNIT = "claude-triage@.service.j2"
RULE = "50-claude-triage.rules.j2"
INSTALLED = "claude_code_agent_user_enabled and claude_code_triage_enabled"
RUNNER = ROLE / "files" / "claude_triage_run.py"


def runner_constants() -> dict:
    """EPISODE_ID's pattern and STATES, read from the runner's source.

    Read rather than imported: a role's `files/` is on no test's path but its own `tests/`.
    """
    found = {}
    for node in ast.parse(RUNNER.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "STATES":
                found[name] = ast.literal_eval(node.value)
            elif name == "EPISODE_ID" and isinstance(node.value, ast.Call):
                found[name] = ast.literal_eval(node.value.args[0])
    assert set(found) == {"STATES", "EPISODE_ID"}, "the runner moved its constants"
    return found


def defaults() -> dict:
    return yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())


def triage_block() -> dict:
    (block,) = yaml_fast.safe_load((ROLE / "tasks" / "triage.yml").read_text())
    return block


def unit(overrides: dict | None = None) -> str:
    # systemd folds a backslash-newline into one line, so ExecStart reads as one directive.
    return re.sub(r"\\\n\s*", " ", render_setup_text("claude_code", UNIT, overrides))


def directive(text: str, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    ]


def test_the_caps_and_identity_are_pinned_above_the_inventory_at_their_defaults() -> (
    None
):
    """Block vars outrank host_vars, so an inventory-only PR cannot raise the caps."""
    pinned = triage_block()["vars"]
    assert set(pinned) == {
        "claude_code_triage_agent_user",
        "claude_code_triage_agent_home",
        "claude_code_triage_state_dir",
        "claude_code_triage_model",
        "claude_code_triage_budget_usd",
        "claude_code_triage_max_turns",
        "claude_code_triage_run_timeout_s",
        "claude_code_triage_daily_cap",
        "claude_code_triage_poll_mode",
    }
    for name, value in pinned.items():
        assert value == defaults()[name], name
        assert "{{" not in str(value), (
            f"{name} templates a variable, so it pins nothing"
        )
    assert (
        pinned["claude_code_triage_agent_user"] == defaults()["claude_code_agent_user"]
    )


def test_the_unit_runs_as_the_agent_with_its_own_credential_and_a_clean_stop() -> None:
    text = unit()
    agent = defaults()["claude_code_agent_user"]
    assert directive(text, "User") == [agent]
    assert directive(text, "EnvironmentFile") == ["/etc/claude-triage/session.env"]
    assert directive(text, "KillSignal") == ["SIGINT"]
    assert directive(text, "OnFailure") == ["claude-triage-alert.service"]
    # A busy lock is a wait, not a failure.
    assert directive(text, "SuccessExitStatus") == ["75"]
    (start,) = directive(text, "ExecStart")
    assert start.startswith("/usr/bin/flock -n -E 75 ")
    assert start.endswith("/opt/claude-triage/claude_triage_run.py %i")


def rule_patterns(rendered: str) -> list[re.Pattern]:
    found = re.findall(r"!/(\^[^/]+\$)/\.test\(unit\)", rendered)
    assert len(found) == 2, "the rule no longer tests both unit shapes"
    return [re.compile(p) for p in found]


def admitted(name: str) -> bool:
    return any(
        p.search(name) for p in rule_patterns(render_setup_text("claude_code", RULE))
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("claude-triage@20261015t1830-memory.service", True),
        ("claude-triage@drill.service", True),
        ("claude-triage-report-alert@drill:diagnosed.service", True),
        ("claude-triage-report-alert@drill:failed.service", True),
        ("claude-triage-report-alert@drill:capped.service", True),
        ("claude-triage@Drill.service", False),
        ("claude-triage@-drill.service", False),
        ("claude-triage@drill.service.d", False),
        ("claude-triage-report-alert@drill:merged.service", False),
        ("claude-triage-report-alert@drill.service", False),
        ("claude-land@3633.service", False),
        ("claude-triage-alert.service", False),
    ],
)
def test_the_rule_admits_only_a_run_and_its_page_in_the_runners_shapes(
    name, expected
) -> None:
    assert admitted(name) is expected


def test_the_rule_and_the_runner_agree_on_the_episode_id_and_states() -> None:
    rendered = render_setup_text("claude_code", RULE)
    constants = runner_constants()
    assert constants["EPISODE_ID"].strip("^$") in rendered
    assert f":({'|'.join(constants['STATES'])})\\.service$" in rendered
    agent = defaults()["claude_code_agent_user"]
    assert f'subject.user !== "{agent}"' in rendered
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


def test_switching_either_flag_off_removes_everything_the_install_writes() -> None:
    tasks = triage_block()["block"]
    installs = [t for t in tasks if t.get("when") == INSTALLED]
    dests = set()
    for task in installs:
        if "ansible.builtin.import_tasks" in task:
            v = task["vars"]
            at = "@" if v.get("alert_unit_instanced") else ""
            dests.add(f"/etc/systemd/system/{v['alert_unit_name']}-alert{at}.service")
            continue
        module = (
            task.get("ansible.builtin.template")
            or task.get("ansible.builtin.file")
            or task.get("ansible.builtin.copy")
        )
        if module is None:
            continue  # the credential assert
        if "loop" in task:
            for i in task["loop"]:
                if isinstance(i, str):
                    dests.add(module["dest"].replace("{{ item }}", i))
                else:
                    dests.add(i.get("dest") or i.get("path"))
        else:
            dests.add(module.get("dest") or module["path"])
    assert "/etc/systemd/system/claude-triage-report-alert@.service" in dests
    assert "/etc/systemd/system/claude-triage-poll.timer" in dests
    assert "/opt/claude-triage/claude_triage_poll.py" in dests
    (removal,) = [
        t
        for t in tasks
        if t.get("when") == f"not ({INSTALLED})" and "ansible.builtin.file" in t
    ]
    removed = removal["loop"]
    state_dir = defaults()["claude_code_triage_state_dir"]
    for dest in dests:
        if dest.startswith((state_dir, "{{ claude_code_triage_state_dir }}")):
            continue  # the records of past runs stay
        assert any(dest == r or dest.startswith(f"{r}/") for r in removed), dest
    assert removed[0].endswith(".rules"), (
        "the rule goes first, so the agent loses the start"
    )


def test_switching_off_stops_the_poller_before_it_removes_anything() -> None:
    """A poll that ran mid-removal could start a run the rule no longer admits."""
    off = [t for t in triage_block()["block"] if t.get("when") == f"not ({INSTALLED})"]
    stop, removal = off
    assert stop["ansible.builtin.systemd_service"] == {
        "name": "claude-triage-poll.timer",
        "enabled": False,
        "state": "stopped",
    }
    assert "ansible.builtin.file" in removal


def test_the_poller_runs_as_the_agent_and_its_timer_is_started() -> None:
    text = re.sub(
        r"\\\n\s*",
        " ",
        render_setup_text("claude_code", "claude-triage-poll.service.j2"),
    )
    assert directive(text, "User") == [defaults()["claude_code_agent_user"]]
    assert directive(text, "OnFailure") == ["claude-triage-poll-alert.service"]
    (start,) = directive(text, "ExecStart")
    assert start.endswith("/opt/claude-triage/claude_triage_poll.py")
    (enable,) = [
        t
        for t in triage_block()["block"]
        if t.get("when") == INSTALLED and "ansible.builtin.systemd_service" in t
    ]
    module = enable["ansible.builtin.systemd_service"]
    assert module["name"] == "claude-triage-poll.timer"
    assert (module["enabled"], module["state"]) == (True, "started")


def test_the_alert_names_and_labels_the_poller_matches_on_exist() -> None:
    """A renamed hold alert or mute label would make the poller's gate match nothing."""
    poller = (ROLE / "files" / "claude_triage_poll.py").read_text()
    assert 'HOLD_ALERT = "DeployHoldActive"' in poller
    observability = ANSIBLE / "roles" / "k8s" / "observability"
    rules = (observability / "files" / "rules" / "deploy-pipeline.yaml").read_text()
    assert "- alert: DeployHoldActive\n" in rules
    secret = yaml_fast.safe_load(
        render_role_template("observability", "alertmanager-secret.yaml.j2")
    )
    routes = yaml_fast.safe_load(secret["stringData"]["alertmanager.yml"])["route"]
    matchers = {m for r in routes["routes"] for m in r.get("matchers", [])}
    for label in ("shadow", "drill"):
        assert f'{label}!="true"' in poller
        assert f'{label}="true"' in matchers


def config() -> str:
    return render_setup_text("claude_code", "claude-triage-config.env.j2")


def test_the_pollers_fallback_address_is_the_pinned_prometheus_cluster_ip() -> None:
    """Prometheus publishes loopback only on its own node; the ClusterIP reaches it anywhere."""
    observability = yaml_fast.safe_load(
        (ANSIBLE / "roles/k8s/observability/defaults/main.yml").read_text()
    )
    ip = observability["observability_prometheus_cluster_ip"]
    assert defaults()["claude_code_triage_prom_urls"] == [
        "http://127.0.0.1:9090",
        f"http://{ip}:9090",
    ]
    assert f"PROM_URLS=http://127.0.0.1:9090 http://{ip}:9090\n" in config()


def test_the_poller_mutes_for_the_whole_weekly_reboot_window() -> None:
    """Five minutes of lead, then the five-minute shutdown delay and 40 of recovery."""
    assert "REBOOT_BEFORE_MIN=5\n" in config()
    assert "REBOOT_GRACE_MIN=45\n" in config()


def test_main_imports_the_triage_file() -> None:
    main = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    assert any(t.get("ansible.builtin.import_tasks") == "triage.yml" for t in main)


TRIAGE_FILES = [
    "ansible/roles/setup/claude_code/tasks/triage.yml",
    f"ansible/roles/setup/claude_code/templates/{UNIT}",
    f"ansible/roles/setup/claude_code/templates/{RULE}",
    "ansible/roles/setup/claude_code/templates/claude-triage-config.env.j2",
    "ansible/roles/setup/claude_code/templates/claude-triage-prompt.txt.j2",
    "ansible/roles/setup/claude_code/templates/claude-triage-poll.service.j2",
    "ansible/roles/setup/claude_code/templates/claude-triage-poll.timer.j2",
    "ansible/roles/setup/claude_code/files/claude_triage_run.py",
    "ansible/roles/setup/claude_code/files/claude_triage_poll.py",
]


@pytest.mark.parametrize("path", TRIAGE_FILES)
def test_a_pr_changing_triage_needs_the_operator(path, tmp_path) -> None:
    """The operator's 2026-10-15 ruling: the agent cannot raise its own caps unattended."""
    assert (ANSIBLE.parent / path).is_file(), f"{path} moved; update this list"
    listed = tmp_path / "approval-paths"
    listed.write_text(render_setup_text("claude_code", "claude-land-approval-paths.j2"))
    floor = policy.read_approval_paths(str(listed))
    assert policy.approval_hits([{"filename": path}], floor) == [path]


def test_a_token_pasted_across_a_wrapped_line_reaches_systemd_whole() -> None:
    """The 2026-10-17 paste carried two spaces from setup-token's line wrap, and Claude 401'd."""
    rendered = render_setup_text(
        "claude_code",
        "claude-triage-session.env.j2",
        {"claude_code_triage_claude_oauth_token": "sk-ant-oat01-abc  def\n"},
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-abcdef\n" in rendered
