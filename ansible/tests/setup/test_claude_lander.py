#!/usr/bin/env python3
"""The agent user lands its own PRs only through homelab-claude-land@<n>.service.

land.sh's landing policy decides what lands.

`roles/setup/claude_code` holds the unit, the polkit rule that lets `claude` start it, and the
approval list land.sh reads, and the claude-code host component ships them. Each test pins one
leg. A leg that slips fails no deploy: the unit still lands PRs, only with more reach than the
design gives the agent.

Run: uv run pytest ansible/tests/setup/test_claude_lander.py
"""

import re
import shlex
from pathlib import Path

import pytest
from _helpers import ANSIBLE
from _setup_render import CLAUDE_CODE, render_component_text, role_context
from deploy_tools.land_lib import handoff, options, policy
from fanout_lib.target import branch_name
from lib.worktree_owner import WORKTREE_PREFIX_ENV
from lib import yaml_fast
from lib.ansible_jinja_env import template_env

ROLE = CLAUDE_CODE
UNIT = "claude-land@.service.j2"
RULE = "50-claude-land.rules.j2"
PATHS = "claude-land-approval-paths.j2"
PINS = "claude-land-pins.j2"
COMPONENT = ROLE / "component.yml"
OPERATOR = "lander-sentinel-operator"
AGENT = "lander-sentinel-agent"


def defaults(directory: Path = ROLE) -> dict:
    return yaml_fast.safe_load((directory / "defaults" / "main.yml").read_text())


def pins() -> dict[str, str]:
    """What claude-land-pins.j2 exports to the templates that import it, by name."""
    env = template_env(ROLE / "templates")
    module = env.get_template(PINS).make_module(role_context(ROLE))
    names = ("unit", "checkout", "branch_prefix", "author", "agent_user", "approver")
    return {name: getattr(module, name) for name in names}


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


def environment(text: str) -> dict[str, str]:
    return dict(e.split("=", 1) for e in directive(text, "Environment"))


def test_the_unit_runs_land_sh_as_the_operator_from_the_primary_checkout() -> None:
    rendered = unit({"sys_user": OPERATOR})
    assert directive(rendered, "User") == [OPERATOR]
    checkout = f"/home/{OPERATOR}/server"
    assert directive(rendered, "WorkingDirectory") == [checkout]
    (exec_start,) = directive(rendered, "ExecStart")
    argv = shlex.split(exec_start)
    land = f"{checkout}/scripts/deploy_tools/land.sh"
    assert land in argv and (ANSIBLE.parent / "scripts/deploy_tools/land.sh").is_file()


def test_a_landing_that_fails_or_is_refused_pages_through_its_alert() -> None:
    """A refused landing waits on the operator, and the session that asked may be gone (#4602)."""
    assert directive(unit(), "OnFailure") == ["homelab-alert@%n.service"]


def test_land_sh_accepts_the_units_arguments_and_writes_the_verdict_where_the_session_reads() -> (
    None
):
    (exec_start,) = directive(unit(), "ExecStart")
    argv = shlex.split(exec_start)
    args = argv[argv.index(next(a for a in argv if a.endswith("/land.sh"))) + 1 :]
    opts = options.parse_args(args, "")
    assert (opts.pr, opts.arm_merge, opts.await_merge) == ("%i", True, True)
    assert not opts.detach and not opts.tags
    state_dir = directive(unit(), "StateDirectory")[0]
    assert opts.verdict_file == f"/var/lib/{state_dir}/%i.verdict"


def test_the_unit_switches_on_every_leg_of_the_landing_policy() -> None:
    env = environment(unit())
    assert (
        env[options.REQUIRE_AUTHOR_ENV] == defaults()["claude_code_agent_github_login"]
    )
    assert env[options.REQUIRE_BRANCH_PREFIX_ENV] == "worktree-claude+"
    (write,) = [
        entry["dst"]
        for entry in yaml_fast.safe_load(COMPONENT.read_text())["files"]
        if entry.get("src") == f"templates/{PATHS}"
    ]
    assert env[options.APPROVAL_PATHS_ENV] == write


def test_only_the_operators_approval_lifts_the_approval_path_refusal() -> None:
    """An approver equal to the agent's own login would let the agent approve itself."""
    approver = environment(unit())[options.APPROVER_ENV]
    assert approver == pins()["approver"]
    assert approver != defaults()["claude_code_agent_github_login"]
    owner = defaults()["claude_code_agent_user_repo"].split("/")[0]
    assert approver == owner, "the operator is the repo's owner"


def test_the_branch_prefix_is_the_one_the_fence_lets_the_agent_push(
    monkeypatch,
) -> None:
    prefix = environment(unit())[options.REQUIRE_BRANCH_PREFIX_ENV]
    fence = defaults(ANSIBLE / "roles" / "setup" / "gitops_deploy")[
        "gitops_deploy_fence_ruleset_exclude"
    ]
    # Master is excluded too, so the agent's account can merge a PR; the lander still lands
    # only the agent's own branches.
    assert fence == ["refs/heads/master", f"refs/heads/{prefix}**"]
    monkeypatch.setenv(
        WORKTREE_PREFIX_ENV, defaults()["claude_code_agent_worktree_prefix"]
    )
    assert branch_name("batch").startswith(prefix)


def rule_pattern(rendered: str) -> re.Pattern:
    found = re.search(r"if \(!/(.+?)/\.test\(action\.lookup\(\"unit\"\)\)\)", rendered)
    assert found, "the rule no longer tests the unit name"
    return re.compile(found.group(1))


@pytest.mark.parametrize(
    ("name", "admitted"),
    [
        ("homelab-claude-land@3633.service", True),
        ("homelab-claude-land@1.service", True),
        ("homelab-claude-land@0.service", False),
        ("homelab-claude-land@12345678.service", False),
        ("homelab-claude-land@36a.service", False),
        ("homelab-claude-land@.service", False),
        ("homelab-claude-land@3633.service.d", False),
        ("renovate-agent-land@3633.service", False),
        ("gitops-deploy.service", False),
    ],
)
def test_the_rule_admits_only_a_landing_unit_for_a_plain_pr_number(
    name, admitted
) -> None:
    assert (
        bool(rule_pattern(render_component_text("claude-code", RULE)).search(name))
        is admitted
    )


def test_the_rule_admits_only_the_agent_user_and_only_the_start_verb() -> None:
    rendered = render_component_text("claude-code", RULE)
    agent = defaults()["claude_code_agent_user"]
    assert f'subject.user !== "{agent}"' in rendered
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


# The agent profile's variables, which the inventory may set, and a value for each that is not
# the agent's.
AGENT_PROFILE_OVERRIDES = {
    "claude_code_agent_user": AGENT,
    "claude_code_agent_github_login": "lander-sentinel-login",
    "claude_code_agent_worktree_prefix": "lander-sentinel-prefix",
}


def test_the_inventory_cannot_repoint_who_the_lander_serves() -> None:
    """Moving the agent profile in the inventory leaves the unit and the rule as they were."""
    assert unit(AGENT_PROFILE_OVERRIDES) == unit()
    assert render_component_text("claude-code", RULE, AGENT_PROFILE_OVERRIDES) == (
        render_component_text("claude-code", RULE)
    )


# The names the role's defaults gave the lander's settings
# before the pins moved into a template, set to values that are not the agent's.
OLD_LANDER_VARS = {
    "claude_code_lander_checkout": "/srv/lander-sentinel",
    "claude_code_lander_author": "lander-sentinel-login",
    "claude_code_lander_branch_prefix": "lander-sentinel-prefix+",
    "claude_code_lander_agent_user": AGENT,
    "claude_code_lander_approver": "lander-sentinel-approver",
}


def test_an_inventory_value_under_the_old_setting_names_moves_nothing() -> None:
    assert unit(OLD_LANDER_VARS) == unit()
    assert render_component_text("claude-code", RULE, OLD_LANDER_VARS) == (
        render_component_text("claude-code", RULE)
    )
    assert set(OLD_LANDER_VARS).isdisjoint(defaults())


def test_the_unit_and_rule_carry_the_pins() -> None:
    """The red half of the two tests above: the templates do read the pins file."""
    pinned = pins()
    env = environment(unit())
    assert env[options.REQUIRE_AUTHOR_ENV] == pinned["author"]
    assert env[options.REQUIRE_BRANCH_PREFIX_ENV] == pinned["branch_prefix"]
    assert env[options.APPROVER_ENV] == pinned["approver"]
    assert directive(unit(), "StateDirectory") == [pinned["unit"]]
    rule = render_component_text("claude-code", RULE)
    assert f'subject.user !== "{pinned["agent_user"]}"' in rule
    assert rule_pattern(rule).pattern.startswith(f"^{pinned['unit']}@")


def test_the_pinned_lander_values_name_the_agent_profile() -> None:
    """The pins are literals, so this is what keeps them equal to the agent they serve."""
    d, pinned = defaults(), pins()
    assert pinned["author"] == d["claude_code_agent_github_login"]
    assert pinned["agent_user"] == d["claude_code_agent_user"]
    assert (
        pinned["branch_prefix"] == f"worktree-{d['claude_code_agent_worktree_prefix']}+"
    )


# The approval floor the operator chose on 2026-10-10: the grant points and the gate itself.
FLOOR = [
    f"hosts/components/claude-code/templates/{PATHS}",
    f"hosts/components/claude-code/templates/{UNIT}",
    f"hosts/components/claude-code/templates/{RULE}",
    # The pins and the component that ships the lander joined with H7.40's second slice.
    f"hosts/components/claude-code/templates/{PINS}",
    "hosts/components/claude-code/component.yml",
    # Alert-triggered triage joined the floor on 2026-10-15 (test_claude_triage.py).
    "hosts/components/claude-code/templates/claude-triage",
    "hosts/components/claude-code/templates/50-claude-triage.rules.j2",
    "hosts/components/claude-code/files/claude_triage_run.py",
    "hosts/components/claude-code/files/claude_triage_poll.py",
    # The root inspector joined the floor with its grant (#6323).
    "hosts/components/claude-code/files/homelab_rootq.py",
    "hosts/components/claude-code/files/rootq.sh",
    "hosts/components/claude-code/templates/claude-rootq.sudoers.j2",
    # The agent's socket to the same inspector joined it on 2026-10-29 (#6350).
    "hosts/components/claude-code/files/homelab_rootq_socket.py",
    "hosts/components/claude-code/files/rootq_socket.py",
    "hosts/components/claude-code/templates/homelab-rootq.socket.j2",
    "hosts/components/claude-code/templates/homelab-rootq@.service.j2",
    "scripts/deploy_tools/land",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    ".python-version",
    ".github/",
    "ansible/roles/setup/initial_setup/tasks/access.yml",
    "ansible/.sops.yaml",
    "ansible/vars/secrets.yml",
    # The agent's cluster RBAC joined the floor on 2026-10-16 (H1 Open question 7).
    "ansible/roles/k8s/cluster-base/",
]


def floor_prefixes(tmp_path) -> list[str]:
    listed = tmp_path / "approval-paths"
    listed.write_text(render_component_text("claude-code", PATHS))
    return policy.read_approval_paths(str(listed))


def test_the_approval_list_land_sh_reads_is_the_floor(tmp_path) -> None:
    assert floor_prefixes(tmp_path) == FLOOR


def test_the_inventory_cannot_override_the_approval_list(tmp_path) -> None:
    """The list is literal in the template, so a host_vars entry has nothing to replace."""
    listed = tmp_path / "approval-paths"
    listed.write_text(
        render_component_text(
            "claude-code", PATHS, {"claude_code_lander_approval_paths": ["docs/"]}
        )
    )
    assert policy.read_approval_paths(str(listed)) == FLOOR


# Each file that decides what the lander does, or grants the agent root-level reach directly.
# A PR changing one must not land without the operator.
LANDER_FILES = [
    f"hosts/components/claude-code/templates/{UNIT}",
    f"hosts/components/claude-code/templates/{RULE}",
    f"hosts/components/claude-code/templates/{PATHS}",
    f"hosts/components/claude-code/templates/{PINS}",
    "hosts/components/claude-code/component.yml",
    "ansible/roles/setup/initial_setup/tasks/access.yml",
    "scripts/deploy_tools/land.sh",
    "scripts/deploy_tools/land.py",
    "scripts/deploy_tools/land_lib/policy.py",
    ".github/workflows/ci.yml",
]


@pytest.mark.parametrize("path", LANDER_FILES)
def test_a_pr_changing_the_lander_needs_the_operator(path, tmp_path) -> None:
    assert (ANSIBLE.parent / path).is_file(), f"{path} moved; update this list"
    assert policy.approval_hits([{"filename": path}], floor_prefixes(tmp_path)) == [
        path
    ]


# H1 creates cluster-base, so these paths do not exist yet and LANDER_FILES cannot hold them.
CLUSTER_BASE_FILE = "ansible/roles/k8s/cluster-base/templates/readonly-rbac.yaml.j2"
CLUSTER_BASE_SIBLING = "ansible/roles/k8s/cluster-baseline/templates/deployment.yaml.j2"


def test_a_pr_changing_cluster_base_needs_the_operator(tmp_path) -> None:
    assert policy.approval_hits(
        [{"filename": CLUSTER_BASE_FILE}], floor_prefixes(tmp_path)
    ) == [CLUSTER_BASE_FILE]


def test_a_pr_changing_a_role_named_like_cluster_base_lands_without_the_operator(
    tmp_path,
) -> None:
    assert (
        policy.approval_hits(
            [{"filename": CLUSTER_BASE_SIBLING}], floor_prefixes(tmp_path)
        )
        == []
    )


# Off the floor since 2026-10-10: the agent's own roles, the deployer and the inventory.
OFF_THE_FLOOR = [
    "docs/landing.md",
    "hosts/components/claude-code/defaults/main.yml",
    "ansible/roles/setup/claude_code/tasks/main.yml",
    "ansible/roles/setup/gitops_deploy/defaults/main.yml",
    "ansible/inventory/host_vars/daniel-box.yml",
]


@pytest.mark.parametrize("path", OFF_THE_FLOOR)
def test_a_pr_off_the_floor_lands_without_the_operator(path, tmp_path) -> None:
    assert (ANSIBLE.parent / path).is_file(), f"{path} moved; update this list"
    assert policy.approval_hits([{"filename": path}], floor_prefixes(tmp_path)) == []


def profile_exports(lander_enabled: bool) -> dict[str, str]:
    rendered = render_component_text(
        "claude-code",
        "agent-user-profile.j2",
        {"claude_code_lander_enabled": lander_enabled},
    )
    return dict(
        line.removeprefix("export ").split("=", 1)
        for line in rendered.splitlines()
        if line.startswith("export ")
    )


def test_the_agents_land_sh_hands_off_to_the_unit_the_rule_lets_it_start() -> None:
    name = profile_exports(True)[options.HANDOFF_ENV]
    shipped = {
        entry["src"]: entry["dst"]
        for entry in yaml_fast.safe_load(COMPONENT.read_text())["files"]
        if "src" in entry
    }
    assert shipped[f"templates/{UNIT}"] == f"/usr/lib/systemd/system/{name}@.service"
    assert rule_pattern(render_component_text("claude-code", RULE)).search(
        f"{name}@3633.service"
    )
    rendered = unit()
    assert directive(rendered, "StateDirectory") == [name]
    (exec_start,) = directive(rendered, "ExecStart")
    argv = shlex.split(exec_start)
    verdict = argv[argv.index("--verdict-file") + 1]
    assert verdict == str(handoff.STATE_ROOT / name / "%i.verdict")


def test_without_the_lander_the_agent_hands_off_nothing() -> None:
    assert options.HANDOFF_ENV not in profile_exports(False)


def test_the_lander_unit_never_hands_off_to_itself() -> None:
    assert options.HANDOFF_ENV not in environment(unit())


def test_the_agents_claude_md_names_the_approval_list_the_lander_reads() -> None:
    doc = " ".join(
        render_component_text(
            "claude-code",
            "agent-user-claude-md.j2",
            {"claude_code_lander_enabled": True},
        ).split()
    )
    approval_paths = environment(unit())[options.APPROVAL_PATHS_ENV]
    assert f"`{approval_paths}`" in doc
    assert (
        "land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>" in doc
    )


def test_land_sh_ends_its_landing_before_systemd_kills_the_unit() -> None:
    """The flock wait comes out of TimeoutStartSec before land.sh starts its own clock (#5199).

    The reboot window can move the promotion wait's cap past LAND_DEADLINE by up to
    LAND_WINDOW_GRACE (#5457), so the unit's limit covers both.
    """
    rendered = unit()
    (timeout,) = directive(rendered, "TimeoutStartSec")
    hours = re.fullmatch(r"(\d+)h", timeout)
    assert hours, f"expected a span in hours, got {timeout!r}"
    (flock_wait,) = re.findall(r"flock -w (\d+) ", directive(rendered, "ExecStart")[0])
    deadline = int(environment(rendered)[options.DEADLINE_ENV])
    grace = int(environment(rendered)[options.WINDOW_GRACE_ENV])
    assert 0 < deadline + grace <= int(hours.group(1)) * 3600 - int(flock_wait)
