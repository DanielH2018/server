#!/usr/bin/env python3
"""The agent user lands its own PRs only through claude-land@<n>.service, under land.sh's policy.

`roles/setup/claude_code` installs the unit, the polkit rule that lets `claude` start it, and the
approval list land.sh reads. Each test pins one leg. A leg that slips fails no deploy: the unit
still lands PRs, only with more reach than the design gives the agent.

Run: uv run pytest ansible/tests/setup/test_claude_lander.py
"""

import re
import shlex

import pytest
from _helpers import ANSIBLE
from _setup_render import render_setup_text
from deploy_tools.land_lib import handoff, options, policy
from fanout_lib.target import branch_name
from lib.worktree_owner import WORKTREE_PREFIX_ENV
from lib import yaml_fast

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
UNIT = "claude-land@.service.j2"
RULE = "50-claude-land.rules.j2"
PATHS = "claude-land-approval-paths.j2"
INSTALLED = "claude_code_agent_user_enabled and claude_code_lander_enabled"
OPERATOR = "lander-sentinel-operator"
AGENT = "lander-sentinel-agent"


def defaults(role: str = "claude_code") -> dict:
    return yaml_fast.safe_load(
        (ANSIBLE / "roles" / "setup" / role / "defaults" / "main.yml").read_text()
    )


def lander_block() -> dict:
    (block,) = yaml_fast.safe_load((ROLE / "tasks" / "lander.yml").read_text())
    return block


def tasks() -> list[dict]:
    """The lander's install and removal tasks, which tasks/lander.yml holds in one block."""
    return lander_block()["block"]


def unit(overrides: dict | None = None) -> str:
    # systemd folds a backslash-newline into one line, so ExecStart reads as one directive.
    return re.sub(r"\\\n\s*", " ", render_setup_text("claude_code", UNIT, overrides))


def directive(text: str, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    ]


def environment(text: str) -> dict[str, str]:
    return dict(e.split("=", 1) for e in directive(text, "Environment"))


def install_dest(task: dict) -> str:
    module = task.get("ansible.builtin.template") or task["ansible.builtin.file"]
    return module.get("dest") or module["path"]


def test_the_unit_runs_land_sh_as_the_operator_from_the_primary_checkout() -> None:
    rendered = unit({"sys_user": OPERATOR})
    assert directive(rendered, "User") == [OPERATOR]
    checkout = defaults()["claude_code_lander_checkout"].replace(
        "{{ sys_user }}", OPERATOR
    )
    assert directive(rendered, "WorkingDirectory") == [checkout]
    (exec_start,) = directive(rendered, "ExecStart")
    argv = shlex.split(exec_start)
    land = f"{checkout}/scripts/deploy_tools/land.sh"
    assert land in argv and (ANSIBLE.parent / "scripts/deploy_tools/land.sh").is_file()


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
        t for t in tasks() if t.get("ansible.builtin.template", {}).get("src") == PATHS
    ]
    assert env[options.APPROVAL_PATHS_ENV] == install_dest(write)


def test_only_the_operators_approval_lifts_the_approval_path_refusal() -> None:
    """An approver equal to the agent's own login would let the agent approve itself."""
    approver = environment(unit())[options.APPROVER_ENV]
    assert approver == defaults()["claude_code_lander_approver"]
    assert approver != defaults()["claude_code_agent_github_login"]
    owner = defaults()["claude_code_agent_user_repo"].split("/")[0]
    assert approver == owner, "the operator is the repo's owner"


def test_the_branch_prefix_is_the_one_the_fence_lets_the_agent_push(
    monkeypatch,
) -> None:
    prefix = environment(unit())[options.REQUIRE_BRANCH_PREFIX_ENV]
    fence = defaults("gitops_deploy")["gitops_deploy_fence_ruleset_exclude"]
    assert fence == [f"refs/heads/{prefix}**"]
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
        ("claude-land@3633.service", True),
        ("claude-land@1.service", True),
        ("claude-land@0.service", False),
        ("claude-land@12345678.service", False),
        ("claude-land@36a.service", False),
        ("claude-land@.service", False),
        ("claude-land@3633.service.d", False),
        ("renovate-agent-land@3633.service", False),
        ("gitops-deploy.service", False),
    ],
)
def test_the_rule_admits_only_a_landing_unit_for_a_plain_pr_number(
    name, admitted
) -> None:
    assert (
        bool(rule_pattern(render_setup_text("claude_code", RULE)).search(name))
        is admitted
    )


def test_the_rule_admits_only_the_agent_user_and_only_the_start_verb() -> None:
    rendered = render_setup_text("claude_code", RULE)
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
    assert render_setup_text("claude_code", RULE, AGENT_PROFILE_OVERRIDES) == (
        render_setup_text("claude_code", RULE)
    )


def test_the_unit_and_rule_follow_the_lander_variables() -> None:
    """The red half of the test above: the templates do read the pinned variables."""
    moved = {
        "claude_code_lander_author": "lander-sentinel-login",
        "claude_code_lander_branch_prefix": "lander-sentinel-prefix+",
        "claude_code_lander_agent_user": AGENT,
    }
    env = environment(unit(moved))
    assert env[options.REQUIRE_AUTHOR_ENV] == "lander-sentinel-login"
    assert env[options.REQUIRE_BRANCH_PREFIX_ENV] == "lander-sentinel-prefix+"
    assert f'subject.user !== "{AGENT}"' in render_setup_text(
        "claude_code", RULE, moved
    )


def test_the_pinned_lander_values_name_the_agent_profile() -> None:
    """The pins are literals, so this is what keeps them equal to the agent they serve."""
    d = defaults()
    assert d["claude_code_lander_author"] == d["claude_code_agent_github_login"]
    assert d["claude_code_lander_agent_user"] == d["claude_code_agent_user"]
    assert d["claude_code_lander_branch_prefix"] == (
        f"worktree-{d['claude_code_agent_worktree_prefix']}+"
    )


# The approval floor the operator chose on 2026-10-10: the grant points and the gate itself.
FLOOR = [
    "ansible/roles/setup/claude_code/tasks/lander.yml",
    f"ansible/roles/setup/claude_code/templates/{PATHS}",
    f"ansible/roles/setup/claude_code/templates/{UNIT}",
    f"ansible/roles/setup/claude_code/templates/{RULE}",
    "scripts/deploy_tools/land",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    ".python-version",
    ".github/",
    "ansible/roles/setup/initial_setup/tasks/access.yml",
    "ansible/.sops.yaml",
    "ansible/vars/secrets.yml",
]


def floor_prefixes(tmp_path) -> list[str]:
    listed = tmp_path / "approval-paths"
    listed.write_text(render_setup_text("claude_code", PATHS))
    return policy.read_approval_paths(str(listed))


def test_the_approval_list_land_sh_reads_is_the_floor(tmp_path) -> None:
    assert floor_prefixes(tmp_path) == FLOOR


def test_the_inventory_cannot_override_the_approval_list(tmp_path) -> None:
    """The list is literal in the template, so a host_vars entry has nothing to replace."""
    listed = tmp_path / "approval-paths"
    listed.write_text(
        render_setup_text(
            "claude_code", PATHS, {"claude_code_lander_approval_paths": ["docs/"]}
        )
    )
    assert policy.read_approval_paths(str(listed)) == FLOOR


# Each file that decides what the lander does, or grants the agent root-level reach directly.
# A PR changing one must not land without the operator.
LANDER_FILES = [
    f"ansible/roles/setup/claude_code/templates/{UNIT}",
    f"ansible/roles/setup/claude_code/templates/{RULE}",
    f"ansible/roles/setup/claude_code/templates/{PATHS}",
    "ansible/roles/setup/claude_code/tasks/lander.yml",
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


# Off the floor since 2026-10-10: the agent's own roles, the deployer and the inventory.
OFF_THE_FLOOR = [
    "docs/landing.md",
    "ansible/roles/setup/claude_code/defaults/main.yml",
    "ansible/roles/setup/claude_code/tasks/main.yml",
    "ansible/roles/setup/gitops_deploy/defaults/main.yml",
    "ansible/inventory/host_vars/daniel-box.yml",
]


@pytest.mark.parametrize("path", OFF_THE_FLOOR)
def test_a_pr_off_the_floor_lands_without_the_operator(path, tmp_path) -> None:
    assert (ANSIBLE.parent / path).is_file(), f"{path} moved; update this list"
    assert policy.approval_hits([{"filename": path}], floor_prefixes(tmp_path)) == []


def test_the_lander_settings_are_pinned_above_the_inventory_at_their_defaults() -> None:
    """Block vars outrank host_vars, so the inventory cannot repoint the unit."""
    pinned = lander_block()["vars"]
    assert set(pinned) == {
        "claude_code_lander_checkout",
        "claude_code_lander_branch_prefix",
        "claude_code_lander_author",
        "claude_code_lander_agent_user",
        "claude_code_lander_approver",
    }
    for name, value in pinned.items():
        assert value == defaults()[name], name
        # A pin that templates an inventory variable resolves from the inventory.
        assert "{{" not in value or value == "/home/{{ sys_user }}/server", name


def test_main_imports_the_lander_file() -> None:
    main = yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())
    assert any(t.get("ansible.builtin.import_tasks") == "lander.yml" for t in main)


def test_switching_either_flag_off_removes_everything_the_install_writes() -> None:
    installs = [t for t in tasks() if t.get("when") == INSTALLED]
    assert {install_dest(t) for t in installs} >= {
        "/etc/claude-land/approval-paths",
        "/etc/systemd/system/claude-land@.service",
        "/etc/polkit-1/rules.d/50-claude-land.rules",
    }
    (removal,) = [t for t in tasks() if t.get("when") == f"not ({INSTALLED})"]
    removed = removal["loop"]
    assert removal["ansible.builtin.file"]["state"] == "absent"
    for dest in map(install_dest, installs):
        assert any(dest == r or dest.startswith(f"{r}/") for r in removed), dest
    assert removed[0].endswith(".rules"), (
        "the rule goes first, so the agent loses the start"
    )


def profile_exports(lander_enabled: bool) -> dict[str, str]:
    rendered = render_setup_text(
        "claude_code",
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
    assert UNIT == f"{name}@.service.j2"
    assert rule_pattern(render_setup_text("claude_code", RULE)).search(
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
        render_setup_text(
            "claude_code",
            "agent-user-claude-md.j2",
            {"claude_code_lander_enabled": True},
        ).split()
    )
    approval_paths = environment(unit())[options.APPROVAL_PATHS_ENV]
    assert f"`{approval_paths}`" in doc
    assert (
        "land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>" in doc
    )
