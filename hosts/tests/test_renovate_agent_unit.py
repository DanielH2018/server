#!/usr/bin/env python3
"""The renovate-agent systemd unit and prompt must keep the guards that bound an unattended run.

This unit spends a Claude session that picks PRs for the lander to land. Four of its lines exist to
defeat failures that produce no error, and each is invisible at deploy time — Ansible reports
the template as applied either way:

1. `flock -n` on ExecStart. Two overlapping runs would fight over the same worktree.
2. `PATH` must name `~/.local/bin`. systemd supplies a minimal PATH exactly as cron does, and
   without it `claude` is not found — the same trap already recorded for the host crons.
3. The arming switch must be wired in BOTH directions. An `enabled: true` with no `false` arm
   is a one-way door: the operator can start the timer and not stop it.
4. The caps come from `templates/renovate-agent-pins.j2`, never from the inventory, and the
   prompt quotes the same pins the unit and config carry. A prompt naming a different PR cap or
   timeout is how a session starts a landing it has no budget to finish.

Every template here is asserted on its RENDER: a substring match on the Jinja holds only while the
expression is spelled that way (#3202).

Run: uv run pytest hosts/tests/test_renovate_agent_unit.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import REPO, K8S_ROLES
from _setup_render import COMPONENTS, render_component_text, role_context
from lib.guard_jinja_env import template_env

NAME = "renovate-agent"
COMPONENT = COMPONENTS / NAME
UNIT = "homelab-renovate-agent.service.j2"
TIMER = "homelab-renovate-agent.timer.j2"
PROMPT = "prompt.txt.j2"
SPEC = yaml_fast.safe_load((COMPONENT / "component.yml").read_text())
DEFAULTS = COMPONENT / "defaults" / "main.yml"


def render(template: str, overrides: dict | None = None) -> str:
    """One of this component's templates rendered at inventory values, `overrides` on top."""
    return render_component_text(NAME, template, overrides)


def directive(unit_text: str, key: str) -> list[str]:
    """Every value assigned to `key`, with systemd's backslash continuations folded in."""
    folded = re.sub(r"\\\n\s*", " ", unit_text)
    return [
        line.split("=", 1)[1].strip()
        for line in folded.splitlines()
        if line.strip().startswith(f"{key}=")
    ]


@pytest.fixture(scope="module")
def unit() -> str:
    """The unit rendered at inventory values."""
    return render(UNIT)


@pytest.fixture(scope="module")
def defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


def test_execstart_serializes_with_a_nonblocking_lock(unit: str) -> None:
    exec_starts = directive(unit, "ExecStart")
    assert exec_starts, "the unit has no ExecStart"
    assert any("flock -n" in e for e in exec_starts), (
        "ExecStart must take the agent's run.lock with -n: two overlapping runs "
        "fight over the same worktree, and a daily timer should say so rather than queue"
    )


def test_execstart_runs_the_wrapper_not_claude_directly(unit: str) -> None:
    """The wrapper is what gates the tick, measures the delta and posts the digest."""
    joined = " ".join(directive(unit, "ExecStart"))
    assert "/usr/lib/homelab/renovate-agent/renovate_agent.py" in joined


def test_path_carries_the_user_local_bin() -> None:
    """`claude` and `uv` live under the session's ~/.local/bin; systemd's default PATH omits it."""
    envs = directive(render(SESSION_UNIT), "Environment")
    paths = [e for e in envs if e.startswith("PATH=")]
    assert paths, "the unit sets no PATH"
    # systemd's last assignment of a variable wins, so the effective PATH is paths[-1].
    assert ".local/bin" in paths[-1]


def test_the_lander_pins_land_sh_to_renovates_prs() -> None:
    """The contract's "never a PR by another author" is held twice: by the lander's own
    check, and by `land.sh --arm-merge`, which reads LAND_REQUIRE_AUTHOR from the lander's
    unit. The login must be the one the wrapper's own census filters on (`RENOVATE_AUTHOR`),
    read from its source rather than typed here."""
    # fact: hosts/components/renovate-agent/CLAUDE.md#Autonomous-role contract (it merges and deploys with no human in the loop)
    envs = directive(render("renovate-agent-land@.service.j2"), "Environment")
    required = [e for e in envs if e.startswith("LAND_REQUIRE_AUTHOR=")]
    assert required, "the unit sets no LAND_REQUIRE_AUTHOR"
    wrapper = (COMPONENT / "files" / "renovate_agent.py").read_text()
    census = re.search(r'^RENOVATE_AUTHOR = "([^"]+)"', wrapper, re.MULTILINE)
    assert census, "the wrapper's RENOVATE_AUTHOR census filter is gone"
    assert required[-1] == f"LAND_REQUIRE_AUTHOR={census.group(1)}"
    lander = (COMPONENT / "files" / "land_renovate_pr.py").read_text()
    assert f'\nAUTHOR = "{census.group(1)}"\n' in lander, (
        "the lander's own author check must name the login the census keys on"
    )


def test_onfailure_pages(unit: str) -> None:
    assert "homelab-alert@%n.service" in directive(unit, "OnFailure")


def test_the_unit_holds_no_webhook(unit: str) -> None:
    """`systemctl show -p ExecStart` serves unit content over the system bus to any local user."""
    assert "discord.com/api/webhooks" not in unit
    assert "discord_alert_webhook" not in unit


def test_unit_timeout_sits_above_the_wrapper_timeout(defaults: dict) -> None:
    """The wrapper's own kill must trip first — it posts a digest, where systemd's kill does not.

    systemd's ceiling kills the whole cgroup, which can take a `land.sh` down mid-Ansible-run.
    """
    unit_timeout = defaults["renovate_agent_unit_timeout"]
    assert unit_timeout.endswith("min"), f"expected minutes, got {unit_timeout}"
    unit_s = int(unit_timeout[:-3]) * 60
    assert unit_s > int(defaults["renovate_agent_run_timeout_s"]) + 300, (
        "TimeoutStartSec must exceed RUN_TIMEOUT_S with room for the two gh censuses"
    )


def test_arming_is_wired_in_both_directions() -> None:
    """A switch that only turns on is a one-way door."""
    link = SPEC["enable"]["homelab-renovate-agent.timer"]
    assert link == {"target": "timers.target", "when": "renovate_agent_enabled"}


def test_the_disarmed_marker_follows_the_arm_flag_both_ways() -> None:
    """RenovateAgentNotReporting goes quiet on the marker, so the marker must track the flag.

    Disarming ships the series the rule's `unless` reads; arming drops the file from the package.
    """
    rules = K8S_ROLES / "observability/files/rules/other-setup-checks.yaml"
    [group] = yaml_fast.safe_load(rules.read_text())["groups"]
    [rule] = [r for r in group["rules"] if r["alert"] == "RenovateAgentNotReporting"]
    [(metric, check)] = re.findall(
        r'unless on \(origin\) \((\w+)\{check="([^"]+)",origin="daniel-box"\} == 1\)',
        rule["expr"],
    )
    [entry] = [
        f
        for f in SPEC["files"]
        if f["dst"].startswith("/var/lib/node-exporter-textfile/")
    ]
    assert entry["dst"].endswith("/renovate-agent-disarmed.prom")
    assert entry["when"] == "not renovate_agent_enabled"
    content = (COMPONENT / entry["src"]).read_text()
    assert re.search(rf'^{metric}\{{check="{check}"\}} 1$', content, re.M)


def test_reconcile_never_restarts_a_session() -> None:
    """A package install must not kill a landing or spend a session as a side effect."""
    assert SPEC["units"]["homelab-renovate-agent.service"] == {"policy": "manual"}
    activates = [
        f
        for f in SPEC["files"]
        if f.get("activate", "").partition(":")[2]
        in ("homelab-renovate-agent.service", "homelab-renovate-agent.timer")
    ]
    assert activates == []


SETUP_UNIT = "homelab-renovate-agent-setup.service"


def test_a_disarmed_agent_still_gets_its_claude_settings() -> None:
    """The guardrail audit watches the settings file whether or not the timer is armed."""
    assert SPEC["enable"][SETUP_UNIT] == "multi-user.target"
    activated = {
        f["src"]
        for f in SPEC["files"]
        if f.get("activate") == f"try-restart:{SETUP_UNIT}"
    }
    assert activated == {
        "files/claude-settings.json",
        "templates/renovate-agent-setup.sh.j2",
    }
    unit = render(f"{SETUP_UNIT}.j2")
    assert directive(unit, "RemainAfterExit") == ["yes"], (
        "try-restart reruns only an active unit"
    )
    assert directive(unit, "ExecStart")[0].endswith(
        " /usr/lib/homelab/renovate-agent-setup"
    )


CONFIG = "config.env.j2"
SESSION_CONF = "session.conf.j2"
SESSION_UNIT = "homelab-renovate-agent-session.service.j2"
PINNED = (
    "max_prs",
    "model",
    "permission_mode",
    "budget_usd",
    "run_timeout_s",
    "unit_timeout",
    "session_timeout_s",
)


def pins() -> dict:
    """What renovate-agent-pins.j2 exports to the templates that import it, by name."""
    env = template_env(COMPONENT / "templates")
    module = env.get_template("renovate-agent-pins.j2").make_module(
        role_context(COMPONENT)
    )
    return {name: getattr(module, name) for name in PINNED}


def test_the_pins_equal_the_defaults(defaults: dict) -> None:
    """The docs fragments read the defaults, so a changed pin has to change its default too."""
    for name, value in pins().items():
        assert defaults[f"renovate_agent_{name}"] == value, name


def test_the_config_unit_and_prompt_quote_the_pins(unit: str) -> None:
    pinned = pins()
    for template, keys in (
        (CONFIG, ("run_timeout_s", "session_timeout_s", "max_prs")),
        (SESSION_CONF, ("model", "permission_mode", "budget_usd")),
    ):
        config = render(template)
        for name in keys:
            assert f"{name.upper()}={pinned[name]}\n" in config, (template, name)
    assert directive(unit, "TimeoutStartSec") == [pinned["unit_timeout"]]
    prompt = render(PROMPT)
    for name in ("max_prs", "session_timeout_s", "budget_usd"):
        assert str(pinned[name]) in prompt, name


SESSION_RULE = "50-renovate-agent-session.rules.j2"
LAND_RULE = "50-renovate-agent-land.rules.j2"


def test_the_session_unit_runs_the_session_script_under_its_wall_clock() -> None:
    """systemd stops the session at session_timeout_s; the runner reads Result=timeout."""
    session = render(SESSION_UNIT, {"renovate_agent_user": "session-sentinel"})
    assert directive(session, "User") == ["renovate-agent"]
    assert directive(session, "TimeoutStartSec") == [str(pins()["session_timeout_s"])]
    assert directive(session, "ExecStart")[0].endswith("/renovate_session.py")
    assert SPEC["units"]["homelab-renovate-agent-session.service"] == {
        "policy": "manual"
    }


@pytest.mark.parametrize("template", [SESSION_RULE, LAND_RULE])
def test_only_the_controller_may_start_the_units(template: str) -> None:
    """dotfiles#967's I1: the session user starts nothing, whatever the inventory says."""
    rule = render(template, {"renovate_agent_user": "session-sentinel"})
    assert 'subject.user !== "renovate-agent-ctl"' in rule
    assert '"renovate-agent"' not in rule


def test_the_session_rule_admits_only_starting_the_session_unit() -> None:
    rule = render(SESSION_RULE)
    assert 'action.lookup("unit") !== "homelab-renovate-agent-session.service"' in rule
    assert 'action.lookup("verb") !== "start"' in rule
    assert rule.count("polkit.Result.YES") == 1


@pytest.mark.parametrize(
    "template",
    [CONFIG, UNIT, PROMPT, SESSION_UNIT, SESSION_CONF, "renovate-agent-setup.sh.j2"],
)
def test_an_inventory_value_moves_no_cap(template: str) -> None:
    """A PR that edits only host_vars must not raise a cap or widen the permission mode."""
    # fact: hosts/components/renovate-agent/CLAUDE.md#Autonomous-role contract (it merges and deploys with no human in the loop)
    moved = {
        "renovate_agent_max_prs": 97,
        "renovate_agent_model": "moved-model",
        "renovate_agent_permission_mode": "bypassPermissions",
        "renovate_agent_budget_usd": 9731,
        "renovate_agent_run_timeout_s": 973100,
        "renovate_agent_unit_timeout": "9731min",
        "renovate_agent_session_timeout_s": 97310,
        "renovate_agent_user": "moved-user",
        "renovate_agent_ctl_home": "/srv/moved-ctl",
    }
    assert render(template, moved) == render(template)


def test_the_prompt_invokes_the_skill() -> None:
    assert render(PROMPT).lstrip().startswith("/renovate-prs")


def test_the_timer_is_persistent() -> None:
    """A host down at the scheduled minute must still run the tick — the backlog only grows."""
    assert "Persistent=true" in render(TIMER)


# ── the verdict: the unit makes no Kuma push, and its deadline straddles the daily period ──

DAY = 86400
CONFIG_ENV = "config.env.j2"
UNIT_DEADLINE = re.compile(
    r'write_check_textfile renovate-agent 1 "\$\(date \+%%s\)" (\d+)'
)


def test_the_unit_and_its_config_make_no_kuma_push(unit: str) -> None:
    """M6 batch B4's Cutover-1a deleted the Renovate Agent tile and dropped the beat with it.

    A beat into a deleted tile gets a 404, `curl -f` fails, and `OnFailure` pages every day.
    """
    posts = directive(unit, "ExecStartPost")
    assert any("write_check_textfile renovate-agent" in p for p in posts), (
        "the unit's textfile write is the verdict RenovateAgentNotReporting reads"
    )
    assert not any("curl" in p or "KUMA_PUSH_URL" in p for p in posts), posts
    assert "/api/push/" not in unit
    assert "KUMA_PUSH_URL" not in render(CONFIG_ENV)


def test_config_env_stays_root_owned_and_group_read_only() -> None:
    """It holds the Discord webhook, which only the controller reads."""
    [output] = [
        o for o in SPEC["outputs"] if o["path"] == "/etc/renovate-agent/config.env"
    ]
    assert (output["owner"], output["group"], output["mode"]) == (
        "root",
        "renovate-agent-ctl",
        "0640",
    )


def test_the_deadline_straddles_the_daily_period(unit: str) -> None:
    """Below one period the rule fires on a run that merely started late; at two periods
    a whole missed run goes unreported. The timer is daily with a 10-min jitter and up to a
    100-min run, so the gap between two verdicts can exceed a day by under two hours."""
    [deadline] = UNIT_DEADLINE.findall(unit)
    assert DAY + 2 * 3600 < int(deadline) < 2 * DAY, (
        f"deadline {deadline}s must sit between one jittered daily period and two days"
    )
    defaults = yaml_fast.safe_load(DEFAULTS.read_text())
    assert re.fullmatch(
        r"\*-\*-\* \d\d:\d\d:\d\d( [\w/]+)?", defaults["renovate_agent_oncalendar"]
    ), (
        "the deadline above assumes a once-daily OnCalendar — re-derive it if the cadence moves"
    )


# ── the denylist exclusion: the prompt must leave a `k8s_autodeploy: false` PR to a person ──

RENOVATE = REPO / "renovate.json"

# The tell is read out of renovate.json, not typed here: the manual rule's groupName is what
# Renovate puts in the PR title, and a rename there that the prompt did not follow is exactly
# the drift this guard exists to catch. The denylist rule leads its parenthetical
# with the tell; a per-package rule whose pin a denied role owns ends its own with it (the
# crowdsec bouncer plugin), so the tell is collected from anywhere inside a
# `(manual …)` parenthetical and every rule must spell it the same way.
_MANUAL_TELL = re.compile(r"\(manual[^)]*?(?P<tell>k8s_autodeploy: [a-z]+)")


def denylist_marker(rules: list[dict]) -> str:
    """The `k8s_autodeploy: …` phrase the denylist rule's groupName carries into a PR title."""
    tells = {
        m.group("tell")
        for r in rules
        if (m := _MANUAL_TELL.search(str(r.get("groupName", ""))))
    }
    assert len(tells) == 1, (
        f"expected one denylist groupName tell, found {sorted(tells)}"
    )
    return tells.pop()


def branch_slug_tell(marker: str) -> str:
    """The marker as it survives in a branch name: Renovate slugifies its groupName.

    A group holding ONE dependency is titled `Update <dep> …` and drops the group name, so the
    branch can be the only place the marker reaches: for example
    `Update klutchell/unbound Docker tag to v1.26.1` on
    `renovate/k8s-image-klutchellunbound-(manual-k8s_autodeploy-false-…)`.
    Every rule carrying the marker sets `groupSingleUpdates: true`, which puts the marker in
    their titles too; a PR raised before that flag existed still titles bare, so both tells stay
    load-bearing.
    Derived rather than typed, for the reason `denylist_marker` is read out of renovate.json:
    a rename of the rule's marker must fail this guard rather than leave the prompt behind.
    """
    return re.sub(r"[^a-z0-9_.]+", "-", marker.lower())


def prompt_exclusion_problems(prompt: str, marker: str) -> list[str]:
    """Every way the prompt can fail to hand a denylisted PR to a person. Empty means it does."""
    problems: list[str] = []
    if marker not in prompt:
        problems.append(
            f"the prompt never names {marker!r}, so the agent works a denylisted PR like any "
            "other manual work order and lands it unattended"
        )
    slug = branch_slug_tell(marker)
    if slug not in prompt:
        problems.append(
            f"the prompt never names {slug!r}, the only place the marker reaches on a "
            "single-dependency group's PR, so a denied role's pin reads as a plain image pin"
        )
    if "headRefName" not in prompt:
        problems.append(
            "the prompt does not tell the agent to read the branch name, so it can only ever "
            "see the marker on the PRs whose title happens to keep it"
        )
    if "land.sh --pr" not in prompt:
        problems.append(
            "the prompt does not hand over the land.sh command, so the digest names a PR "
            "left open with nothing a person can run"
        )
    return problems


def test_the_prompt_leaves_a_denylisted_pr_to_a_person() -> None:
    rules = __import__("json").loads(RENOVATE.read_text())["packageRules"]
    problems = prompt_exclusion_problems(render(PROMPT), denylist_marker(rules))
    assert not problems, "\n".join(problems)


_TITLE_TELL = "titled `k8s_autodeploy: false`"
_SLUG_TELL = "branched `k8s_autodeploy-false`"
_BRANCH_READ = "read `gh pr list --json number,title,headRefName`"
_LAND = "report `land.sh --pr <n>`"


def test_a_prompt_naming_both_tells_and_the_command_is_clean() -> None:
    prompt = f"- leave a PR {_TITLE_TELL} or {_SLUG_TELL}; {_BRANCH_READ}; {_LAND}"
    assert prompt_exclusion_problems(prompt, "k8s_autodeploy: false") == []


@pytest.mark.parametrize(
    ("dropped", "fragment"),
    [
        (_TITLE_TELL, "'k8s_autodeploy: false'"),
        (_SLUG_TELL, "'k8s_autodeploy-false'"),
        (_BRANCH_READ, "read the branch name"),
        (_LAND, "land.sh command"),
    ],
)
def test_a_prompt_missing_any_one_of_them_is_flagged(
    dropped: str, fragment: str
) -> None:
    """Each half is checked on its own: a prompt naming three of the four is not clean."""
    parts = [_TITLE_TELL, _SLUG_TELL, _BRANCH_READ, _LAND]
    prompt = "- leave a PR " + "; ".join(p for p in parts if p != dropped)
    problems = prompt_exclusion_problems(prompt, "k8s_autodeploy: false")
    assert len(problems) == 1 and fragment in problems[0], problems


def test_the_branch_slug_tell_is_the_marker_renovate_would_put_in_a_branch() -> None:
    """`: ` becomes `-`; the underscore and the word survive."""
    assert branch_slug_tell("k8s_autodeploy: false") == "k8s_autodeploy-false"


def test_the_marker_is_read_from_the_rule_not_typed_here() -> None:
    """A groupName without the tell leaves nothing to assert, and must say so."""
    with pytest.raises(AssertionError, match="expected one denylist groupName tell"):
        denylist_marker([{"groupName": "k8s image {{depName}}"}])


def test_a_rule_spelling_the_tell_differently_fails_rather_than_passing_one_of_them() -> (
    None
):
    """Two spellings are two markers, and the prompt can name only one of them."""
    with pytest.raises(AssertionError, match="expected one denylist groupName tell"):
        denylist_marker(
            [
                {
                    "groupName": "k8s image {{depName}} (manual — k8s_autodeploy: false, …)"
                },
                {"groupName": "plugin (manual — finish it; k8s_autodeploy: off)"},
            ]
        )


def test_the_tell_is_read_from_the_end_of_a_per_package_parenthetical() -> None:
    """A per-package rule ends its parenthetical with the tell; both rules must yield one."""
    assert (
        denylist_marker(
            [
                {
                    "groupName": "k8s image {{depName}} (manual — k8s_autodeploy: false, …)"
                },
                {"groupName": "plugin (manual — finish it; k8s_autodeploy: false)"},
            ]
        )
        == "k8s_autodeploy: false"
    )


def test_the_session_and_the_controller_meet_in_one_exchange_directory() -> None:
    """The session writes where the prompt says, and the runner reads where its config says."""
    [(prompt_dir,)] = [
        m.groups() for m in [re.search(r"`(/\S+)/requests/`", render(PROMPT))] if m
    ]
    setup = render("renovate-agent-setup.sh.j2")
    for template in (CONFIG, SESSION_CONF):
        assert f"\nEXCHANGE_DIR={prompt_dir}\n" in render(template), template
    assert re.search(
        rf'^install -d -o "\$ctl" -g "\$agent" -m 2750 {re.escape(prompt_dir)}$',
        setup,
        re.MULTILINE,
    ), "the setup must create the exchange the runner and the session use"
