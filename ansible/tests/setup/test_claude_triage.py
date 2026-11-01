#!/usr/bin/env python3
"""The agent's unattended triage unit runs capped, as the agent, and only the agent may start it.

The claude-code host component ships claude-triage@.service, its polkit rule and its two pages
from `hosts/components/claude-code/templates/`. Each test pins one leg of the design in docs/claude-agent-user.md,
"Alert-triggered triage". The runner's own behaviour is tested beside it, in
hosts/components/claude-code/tests/test_claude_triage_run.py.

Run: uv run pytest ansible/tests/setup/test_claude_triage.py
"""

import ast
import re

import pytest
from _helpers import ANSIBLE
from _k8s_render import render_role_template
from _setup_render import CLAUDE_CODE, render_component_text, role_context
from deploy_tools.land_lib import policy
from lib.ansible_jinja_env import template_env
from lib import yaml_fast

ROLE = CLAUDE_CODE
UNIT = "claude-triage@.service.j2"
RULE = "50-claude-triage.rules.j2"
RUNNER = ROLE / "files" / "claude_triage_run.py"
CONFIG = "claude-triage-config.env.j2"
# Each pin claude-triage-pins.j2 exports, and the role variable that carried it before.
PINNED = {
    "agent_user": "claude_code_triage_agent_user",
    "agent_home": "claude_code_triage_agent_home",
    "state_dir": "claude_code_triage_state_dir",
    "model": "claude_code_triage_model",
    "budget_usd": "claude_code_triage_budget_usd",
    "max_turns": "claude_code_triage_max_turns",
    "run_timeout_s": "claude_code_triage_run_timeout_s",
    "daily_cap": "claude_code_triage_daily_cap",
    "poll_mode": "claude_code_triage_poll_mode",
    "poll_min_severity": "claude_code_triage_poll_min_severity",
}


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


def unit(overrides: dict | None = None) -> str:
    # systemd folds a backslash-newline into one line, so ExecStart reads as one directive.
    return re.sub(
        r"\\\n\s*", " ", render_component_text("claude-code", UNIT, overrides)
    )


def directive(text: str, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    ]


def pins() -> dict:
    """What claude-triage-pins.j2 exports to the templates that import it, by name."""
    env = template_env(ROLE / "templates")
    module = env.get_template("claude-triage-pins.j2").make_module(role_context(ROLE))
    return {name: getattr(module, name) for name in PINNED}


def test_the_template_pins_equal_the_defaults() -> None:
    """The host package reads the pins, so a changed default has to change them too."""
    for name, var in PINNED.items():
        assert pins()[name] == defaults()[var], name


# Inventory values under the role's names, none of them the pinned ones. The package renderer
# puts the inventory above a component's defaults, so only a template's own pin holds here.
RAISED = {
    "claude_code_triage_agent_user": "triage-sentinel-user",
    "claude_code_triage_state_dir": "/srv/triage-sentinel",
    "claude_code_triage_budget_usd": 999,
    "claude_code_triage_max_turns": 999,
    "claude_code_triage_run_timeout_s": 99999,
    "claude_code_triage_daily_cap": 99,
    "claude_code_triage_poll_mode": "armed",
    "claude_code_triage_poll_min_severity": "none",
}


@pytest.mark.parametrize(
    "template", [UNIT, RULE, CONFIG, "claude-triage-poll.service.j2"]
)
def test_an_inventory_value_under_the_role_names_moves_nothing(template) -> None:
    assert render_component_text(
        "claude-code", template, RAISED
    ) == render_component_text("claude-code", template)


def test_the_config_carries_the_pins() -> None:
    """The red half of the test above: the config does read the pins file."""
    pinned = pins()
    config = dict(
        line.split("=", 1)
        for line in render_component_text("claude-code", CONFIG).splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert config["BUDGET_USD"] == str(pinned["budget_usd"])
    assert config["DAILY_CAP"] == str(pinned["daily_cap"])
    assert config["POLL_MODE"] == pinned["poll_mode"]
    assert config["POLL_MIN_SEVERITY"] == pinned["poll_min_severity"]
    assert config["STATE_DIR"] == pinned["state_dir"]


def test_the_unit_runs_as_the_agent_with_its_own_credential_and_a_clean_stop() -> None:
    text = unit()
    agent = defaults()["claude_code_agent_user"]
    assert directive(text, "User") == [agent]
    assert directive(text, "LoadCredential") == [
        "claude_oauth_token:/etc/claude-triage/oauth-token"
    ]
    assert directive(text, "KillSignal") == ["SIGINT"]
    assert directive(text, "OnFailure") == ["homelab-alert@%n.service"]
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
        p.search(name)
        for p in rule_patterns(render_component_text("claude-code", RULE))
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
        ("homelab-claude-land@3633.service", False),
        ("claude-triage-alert.service", False),
    ],
)
def test_the_rule_admits_only_a_run_and_its_page_in_the_runners_shapes(
    name, expected
) -> None:
    assert admitted(name) is expected


def test_the_rule_and_the_runner_agree_on_the_episode_id_and_states() -> None:
    rendered = render_component_text("claude-code", RULE)
    constants = runner_constants()
    assert constants["EPISODE_ID"].strip("^$") in rendered
    assert f":({'|'.join(constants['STATES'])})\\.service$" in rendered
    agent = defaults()["claude_code_agent_user"]
    assert f'subject.user !== "{agent}"' in rendered
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


def test_the_poller_runs_as_the_agent() -> None:
    text = re.sub(
        r"\\\n\s*",
        " ",
        render_component_text("claude-code", "claude-triage-poll.service.j2"),
    )
    assert directive(text, "User") == [defaults()["claude_code_agent_user"]]
    assert directive(text, "OnFailure") == ["homelab-claude-triage-poll-alert.service"]
    (start,) = directive(text, "ExecStart")
    assert start.endswith("/opt/claude-triage/claude_triage_poll.py")


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
    return render_component_text("claude-code", "claude-triage-config.env.j2")


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


TRIAGE_FILES = [
    f"hosts/components/claude-code/templates/{UNIT}",
    f"hosts/components/claude-code/templates/{RULE}",
    "hosts/components/claude-code/templates/claude-triage-config.env.j2",
    "hosts/components/claude-code/templates/claude-triage-prompt.txt.j2",
    "hosts/components/claude-code/templates/claude-triage-poll.service.j2",
    "hosts/components/claude-code/templates/claude-triage-poll.timer.j2",
    "hosts/components/claude-code/files/claude_triage_run.py",
    "hosts/components/claude-code/files/claude_triage_poll.py",
]


@pytest.mark.parametrize("path", TRIAGE_FILES)
def test_a_pr_changing_triage_needs_the_operator(path, tmp_path) -> None:
    """The operator's 2026-10-15 ruling: the agent cannot raise its own caps unattended."""
    assert (ANSIBLE.parent / path).is_file(), f"{path} moved; update this list"
    listed = tmp_path / "approval-paths"
    listed.write_text(
        render_component_text("claude-code", "claude-land-approval-paths.j2")
    )
    floor = policy.read_approval_paths(str(listed))
    assert policy.approval_hits([{"filename": path}], floor) == [path]


@pytest.mark.parametrize("template", [UNIT, "claude-triage-poll.service.j2"])
def test_what_the_triage_units_write_is_kept_from_the_operators_group(template) -> None:
    """The operator traverses the state dir, so a file there is reachable by its fixed name."""
    text = render_component_text("claude-code", template)
    assert directive(text, "UMask") == ["0027"]


def tmpfiles(operator_read: bool) -> dict[str, tuple[str, str, str]]:
    """Each directory the tmpfiles file declares: path -> (mode, owner, group)."""
    text = render_component_text(
        "claude-code",
        "claude-triage-tmpfiles.conf.j2",
        {
            "sys_user": "ubuntu",
            "claude_code_agent_operator_read": operator_read,
        },
    )
    return {
        fields[1]: (fields[2], fields[3], fields[4])
        for fields in (line.split() for line in text.splitlines())
        if fields and fields[0] == "d"
    }


@pytest.mark.parametrize(
    "operator_read, group, mode",
    [(True, "ubuntu", "0710"), (False, "claude", "0750")],
)
def test_the_operator_traverses_the_triage_state_dir_only_under_operator_read(
    operator_read, group, mode
) -> None:
    """0710 lets `probe.py applied` stat the inbox without listing the records (#6452)."""
    state_dir = pins()["state_dir"]
    assert tmpfiles(operator_read)[state_dir] == (mode, "claude", group)


def test_the_runners_own_directories_are_declared_without_a_listing() -> None:
    """`runs/` and `done/` made under the old 0022 umask were 0755; tmpfiles resets them."""
    declared = tmpfiles(True)
    state_dir = pins()["state_dir"]
    assert declared[f"{state_dir}/runs"][0] == "0750"
    assert declared[f"{state_dir}/done"][0] == "0750"
