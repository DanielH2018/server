#!/usr/bin/env python3
"""Unattended triage runs capped, as its own controller and session users, and only the controller
may start it.

The claude-code host component ships claude-triage@.service, its polkit rule and its two pages
from `hosts/components/claude-code/templates/`. Each test pins one leg of the design in docs/claude-agent-user.md,
"Alert-triggered triage". The runner's own behaviour is tested beside it, in
hosts/components/claude-code/tests/test_claude_triage_run.py.

Run: uv run pytest hosts/tests/test_claude_triage.py
"""

import ast
import json
import re

import pytest
from _helpers import REPO, K8S_ROLES
from _k8s_render import render_role_template
from _setup_render import CLAUDE_CODE, render_component_text, role_context
from deploy_tools.land_lib import policy
from dev.fanout_lib.telemetry import SessionKind
from lib.guard_jinja_env import template_env
from lib import yaml_fast

ROLE = CLAUDE_CODE
UNIT = "claude-triage@.service.j2"
SESSION_UNIT = "claude-triage-session.service.j2"
RULE = "50-claude-triage.rules.j2"
RUNNER = ROLE / "files" / "claude_triage_run.py"
CONFIG = "claude-triage-config.env.j2"
# Each pin claude-triage-pins.j2 exports, and the role variable that carried it before.
PINNED = {
    "state_dir": "claude_code_triage_state_dir",
    "model": "claude_code_triage_model",
    "budget_usd": "claude_code_triage_budget_usd",
    "max_turns": "claude_code_triage_max_turns",
    "run_timeout_s": "claude_code_triage_run_timeout_s",
    "daily_cap": "claude_code_triage_daily_cap",
    "poll_mode": "claude_code_triage_poll_mode",
    "poll_routes": "claude_code_triage_poll_routes",
}


def runner_constants() -> dict:
    """EPISODE_ID's pattern, STATES and the two wait margins, read from the runner's source.

    Read rather than imported: a role's `files/` is on no test's path but its own `tests/`.
    """
    found = {}
    for node in ast.parse(RUNNER.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("STATES", "SESSION_WAIT_MARGIN_S", "RESTART_WAIT_MARGIN_S"):
                found[name] = ast.literal_eval(node.value)
            elif name == "EPISODE_ID" and isinstance(node.value, ast.Call):
                found[name] = ast.literal_eval(node.value.args[0])
    assert set(found) == {
        "STATES",
        "EPISODE_ID",
        "SESSION_WAIT_MARGIN_S",
        "RESTART_WAIT_MARGIN_S",
    }, "the runner moved its constants"
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
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    for name, var in PINNED.items():
        assert pins()[name] == defaults()[var], name


# Inventory values under the role's names, none of them the pinned ones. The package renderer
# puts the inventory above a component's defaults, so only a template's own pin holds here.
RAISED = {
    "claude_code_triage_agent_user": "triage-sentinel-user",
    "claude_code_agent_user": "agent-sentinel-user",
    "claude_code_triage_state_dir": "/srv/triage-sentinel",
    "claude_code_triage_budget_usd": 999,
    "claude_code_triage_max_turns": 999,
    "claude_code_triage_run_timeout_s": 99999,
    "claude_code_triage_daily_cap": 99,
    "claude_code_triage_poll_mode": "armed",
    "claude_code_triage_poll_routes": {"HostLogErrors": "triage"},
}


@pytest.mark.parametrize(
    "template",
    [
        UNIT,
        RULE,
        CONFIG,
        SESSION_UNIT,
        "claude-triage-poll.service.j2",
        "claude-triage-tmpfiles.conf.j2",
    ],
)
def test_an_inventory_value_under_the_role_names_moves_nothing(template) -> None:
    assert render_component_text(
        "claude-code", template, RAISED
    ) == render_component_text("claude-code", template)


def config_env(overrides: dict | None = None) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in render_component_text("claude-code", CONFIG, overrides).splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_the_config_carries_the_pins() -> None:
    """The red half of the test above: the config does read the pins file."""
    pinned = pins()
    config = config_env()
    assert config["BUDGET_USD"] == str(pinned["budget_usd"])
    assert config["DAILY_CAP"] == str(pinned["daily_cap"])
    assert config["POLL_MODE"] == pinned["poll_mode"]
    assert json.loads(config["POLL_ROUTES"]) == pinned["poll_routes"]
    assert config["STATE_DIR"] == pinned["state_dir"]


def test_the_run_outlasts_the_session_and_the_restart_it_waits_for() -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    config = config_env({"claude_code_restart_enabled": True})
    broker = json.loads(
        render_component_text("claude-code", "claude-restart-config.json.j2")
    )
    broker_unit = render_component_text("claude-code", "claude-restart@.service.j2")
    session = render_component_text("claude-code", SESSION_UNIT)
    assert json.loads(config["RESTART_NAMES"]) == sorted(broker["allowlist"])
    assert config["RESTART_VERDICT"] == f"{broker['state_dir']}/verdict"
    assert directive(broker_unit, "TimeoutStartSec") == [config["RESTART_TIMEOUT_S"]]
    margins = runner_constants()
    waits = int(directive(session, "TimeoutStartSec")[0])
    waits += margins["SESSION_WAIT_MARGIN_S"]
    waits += int(config["RESTART_TIMEOUT_S"]) + margins["RESTART_WAIT_MARGIN_S"]
    assert int(directive(unit(), "TimeoutStartSec")[0]) > waits


def test_a_switched_off_broker_leaves_a_run_no_name_to_ask_for() -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`homelab-claude-restart@`, a restart of one allowlisted Deployment)
    off = {"claude_code_restart_enabled": False}
    assert json.loads(config_env(off)["RESTART_NAMES"]) == []
    prompt = render_component_text("claude-code", "claude-triage-prompt.txt.j2", off)
    assert "Leave the `restart` field out." in prompt
    assert '"docs"' not in prompt


# The pinned users (claude-triage-pins.j2): the controller and the session.
CTL_USER = "claude-triage-ctl"
SESSION_USER = "claude-triage"


def test_the_unit_runs_as_the_agent_with_its_own_credential_and_a_clean_stop() -> None:
    text = unit()
    assert directive(text, "User") == [CTL_USER]
    # The controller reads the session's result through the session user's group.
    assert directive(text, "SupplementaryGroups") == [SESSION_USER]
    # The session unit holds the credential; the runner never needs it.
    assert directive(text, "LoadCredential") == []
    session = re.sub(
        r"\\\n\s*", " ", render_component_text("claude-code", SESSION_UNIT)
    )
    assert directive(session, "User") == [SESSION_USER]
    assert directive(session, "LoadCredential") == [
        "claude_oauth_token:/etc/claude-triage/oauth-token"
    ]
    assert directive(session, "KillSignal") == ["SIGINT"]
    # The session's own wall clock trips before the runner's, so the runner still records it.
    assert int(directive(session, "TimeoutStartSec")[0]) < int(
        directive(text, "TimeoutStartSec")[0]
    )
    assert directive(session, "ExecStart")[0].endswith(
        "/opt/claude-triage/claude_triage_session.py"
    )
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
    rendered = render_component_text("claude-code", RULE)
    exact = re.findall(r'unit !== "([^"]+)"', rendered)
    return name in exact or any(p.search(name) for p in rule_patterns(rendered))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("claude-triage@20261015t1830-memory.service", True),
        ("claude-triage@drill.service", True),
        ("claude-triage-report-alert@drill:diagnosed.service", True),
        ("claude-triage-report-alert@drill:failed.service", True),
        ("claude-triage-report-alert@drill:capped.service", True),
        ("homelab-claude-triage-session.service", True),
        ("homelab-claude-triage-session@drill.service", False),
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
    assert f'subject.user !== "{CTL_USER}"' in rendered
    # Neither the agent, whose interactive sessions read untrusted text, nor the session user.
    assert f'"{defaults()["claude_code_agent_user"]}"' not in rendered
    assert f'"{SESSION_USER}"' not in rendered
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


def test_the_artifacts_pod_mounts_the_report_tree_the_host_creates() -> None:
    """The host plane creates the tree and the Flux plane mounts it; one path, two owners."""
    # fact: k8s/roles/artifacts/CLAUDE.md#At a glance
    env = template_env(ROLE / "templates")
    module = env.get_template("claude-triage-pins.j2").make_module(role_context(ROLE))
    report_dir, ctl_home = (getattr(module, n) for n in ("report_dir", "ctl_home"))
    agent_home = defaults()["claude_code_agent_user_home"]
    deployment = yaml_fast.safe_load(
        render_role_template(
            "artifacts", "deployment.yaml.j2", {"claude_code_triage_enabled": True}
        )
    )
    paths = {
        v["hostPath"]["path"]
        for v in deployment["spec"]["template"]["spec"]["volumes"]
        if "hostPath" in v
    }
    assert report_dir in paths
    tmpfiles = render_component_text("claude-code", "claude-triage-tmpfiles.conf.j2")
    assert re.search(rf"^d {re.escape(report_dir)} 2750 ", tmpfiles, re.M)
    assert report_dir.startswith(ctl_home + "/")
    assert not ctl_home.startswith(agent_home + "/")


def test_the_poller_runs_as_the_controller() -> None:
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    text = re.sub(
        r"\\\n\s*",
        " ",
        render_component_text("claude-code", "claude-triage-poll.service.j2"),
    )
    assert directive(text, "User") == [CTL_USER]
    assert directive(text, "OnFailure") == ["homelab-claude-triage-poll-alert.service"]
    (start,) = directive(text, "ExecStart")
    assert start.endswith("/opt/claude-triage/claude_triage_poll.py")


def test_the_alert_names_and_labels_the_poller_matches_on_exist() -> None:
    """A renamed hold alert or mute label would make the poller's gate match nothing."""
    poller = (ROLE / "files" / "claude_triage_poll.py").read_text()
    assert 'HOLD_ALERT = "DeployHoldActive"' in poller
    observability = K8S_ROLES / "observability"
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
        (K8S_ROLES / "observability/defaults/main.yml").read_text()
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


# What the component ships for triage that may change without the operator, and why.
TRIAGE_OFF_THE_FLOOR = {
    # It pages a failing poller; it reads no pin and starts nothing.
    "units/homelab-claude-triage-poll-alert.service": "a page with no input a run sets",
}


def _triage_files() -> list[str]:
    """Every file the component ships for triage, from component.yml, plus the pins it imports.

    Derived, so a triage file added to the package without a line on the approval list fails here.
    """
    spec = yaml_fast.safe_load((ROLE / "component.yml").read_text())
    shipped = sorted(
        f"hosts/components/claude-code/{e['src']}"
        for e in spec["files"]
        if "triage" in e.get("src", "") and e["src"] not in TRIAGE_OFF_THE_FLOOR
    )
    return [*shipped, "hosts/components/claude-code/templates/claude-triage-pins.j2"]


TRIAGE_FILES = _triage_files()


def test_the_triage_file_census_names_the_runner_the_session_and_the_rule() -> None:
    assert {
        "hosts/components/claude-code/files/claude_triage_run.py",
        "hosts/components/claude-code/files/claude_triage_session.py",
        "hosts/components/claude-code/files/claude-triage-settings-overlay.json",
        f"hosts/components/claude-code/templates/{RULE}",
        "hosts/components/claude-code/templates/claude-triage-setup.sh.j2",
    } <= set(TRIAGE_FILES)


def test_the_sessions_overlay_is_renovates_with_the_hooks_moved_to_the_controllers_tools() -> (
    None
):
    """One server floor for both unattended sessions; only where the hook scripts live differs."""
    renovate = json.loads(
        (
            REPO / "hosts/components/renovate-agent/files/claude-settings-overlay.json"
        ).read_text()
    )
    triage = json.loads(
        (ROLE / "files" / "claude-triage-settings-overlay.json").read_text()
    )
    assert triage["permissions"] == renovate["permissions"]
    moved = json.loads(
        json.dumps(renovate["hooks"]).replace(
            '"/var/lib/renovate-agent-tools/', '"/var/lib/claude-triage-tools/'
        )
    )
    assert triage["hooks"] == moved


@pytest.mark.parametrize(
    ("overlay", "kind"),
    [
        (ROLE / "files" / "claude-triage-settings-overlay.json", SessionKind.TRIAGE),
        (
            REPO / "hosts/components/renovate-agent/files/claude-settings-overlay.json",
            SessionKind.RENOVATE,
        ),
    ],
)
def test_each_unattended_session_exports_telemetry_to_the_loopback_collector_under_its_kind(
    overlay, kind
) -> None:
    env = json.loads(overlay.read_text())["env"]
    context = role_context(ROLE)
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_METRICS_EXPORTER"] == env["OTEL_LOGS_EXPORTER"] == "otlp"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        f"http://{context['claude_code_otlp_host_ip']}:{context['claude_code_otlp_port']}"
    )
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == f"session.kind={kind}"
    assert not [name for name in env if name.startswith("OTEL_LOG_")], (
        "session text stays out of Loki"
    )


@pytest.mark.parametrize("path", TRIAGE_FILES)
def test_a_pr_changing_triage_needs_the_operator(path, tmp_path) -> None:
    """The operator's 2026-10-15 ruling: the agent cannot raise its own caps unattended."""
    assert (REPO / path).is_file(), f"{path} moved; update this list"
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
    [(True, "ubuntu", "0710"), (False, CTL_USER, "0750")],
)
def test_the_operator_traverses_the_triage_state_dir_only_under_operator_read(
    operator_read, group, mode
) -> None:
    """0710 lets `probe.py applied` stat the inbox without listing the records."""
    state_dir = pins()["state_dir"]
    assert tmpfiles(operator_read)[state_dir] == (mode, CTL_USER, group)


def test_the_runners_own_directories_are_declared_without_a_listing() -> None:
    """`runs/` and `done/` made under a 0022 umask are 0755; tmpfiles resets them."""
    declared = tmpfiles(True)
    state_dir = pins()["state_dir"]
    assert declared[f"{state_dir}/runs"][0] == "0750"
    assert declared[f"{state_dir}/done"][0] == "0750"


def test_no_triage_path_sits_where_the_agent_or_the_session_could_rename_it() -> None:
    """A path inside a home its user owns can be renamed and replaced by that user (I1)."""
    pinned = {
        name: getattr(
            template_env(ROLE / "templates")
            .get_template("claude-triage-pins.j2")
            .make_module(role_context(ROLE)),
            name,
        )
        for name in (
            "state_dir",
            "exchange_dir",
            "tools_dir",
            "clone_dir",
            "report_dir",
        )
    }
    homes = (defaults()["claude_code_agent_user_home"], "/var/lib/claude-triage")
    for name, path in pinned.items():
        assert not any(path.startswith(home + "/") for home in homes), name


def test_the_session_shares_only_read_access_with_the_controller() -> None:
    """The session reads the clone, the toolchain and the exchange through its group; tmpfiles
    gives it no write on any of them. The runner recreates the exchange's out/ for each run, and
    the session's home is only the mountpoint of its per-run tmpfs."""
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)
    declared = tmpfiles(True)
    module = (
        template_env(ROLE / "templates")
        .get_template("claude-triage-pins.j2")
        .make_module(role_context(ROLE))
    )
    expected = {
        "exchange_dir": ("2750", CTL_USER, SESSION_USER),
        "tools_dir": ("2750", CTL_USER, SESSION_USER),
        "clone_dir": ("2750", CTL_USER, SESSION_USER),
        "session_home": ("0755", "root", "root"),
    }
    for name, entry in expected.items():
        assert declared[getattr(module, name)] == entry, name
