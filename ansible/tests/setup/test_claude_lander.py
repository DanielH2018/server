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


def tasks() -> list[dict]:
    return yaml_fast.safe_load((ROLE / "tasks" / "main.yml").read_text())


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
    rendered = render_setup_text("claude_code", RULE, {"claude_code_agent_user": AGENT})
    assert f'subject.user !== "{AGENT}"' in rendered
    assert 'action.lookup("verb") !== "start"' in rendered
    assert rendered.count("polkit.Result.YES") == 1


def test_the_approval_list_land_sh_reads_is_the_one_the_defaults_declare(
    tmp_path,
) -> None:
    listed = tmp_path / "approval-paths"
    listed.write_text(render_setup_text("claude_code", PATHS))
    assert (
        policy.read_approval_paths(str(listed))
        == defaults()["claude_code_lander_approval_paths"]
    )


# Each file that decides what the lander does. A PR changing one widens what the agent can
# land, so the agent must not be able to land that PR itself.
LANDER_FILES = [
    f"ansible/roles/setup/claude_code/templates/{UNIT}",
    f"ansible/roles/setup/claude_code/templates/{RULE}",
    f"ansible/roles/setup/claude_code/templates/{PATHS}",
    "ansible/roles/setup/claude_code/defaults/main.yml",
    "ansible/roles/setup/claude_code/tasks/main.yml",
    "ansible/inventory/host_vars/daniel-box.yml",
    "ansible/roles/setup/gitops_deploy/defaults/main.yml",
    "scripts/deploy_tools/land.sh",
    "scripts/deploy_tools/land.py",
    "scripts/deploy_tools/land_lib/policy.py",
    ".github/workflows/ci.yml",
]


@pytest.mark.parametrize("path", LANDER_FILES)
def test_a_pr_changing_the_lander_needs_the_operator(path) -> None:
    assert (ANSIBLE.parent / path).is_file(), f"{path} moved; update this list"
    prefixes = defaults()["claude_code_lander_approval_paths"]
    assert policy.approval_hits([{"filename": path}], prefixes) == [path]


def test_a_docs_pr_does_not_need_the_operator() -> None:
    prefixes = defaults()["claude_code_lander_approval_paths"]
    assert policy.approval_hits([{"filename": "docs/landing.md"}], prefixes) == []


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
