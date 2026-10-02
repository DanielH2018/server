#!/usr/bin/env python3
"""The renovate-agent systemd unit and prompt must keep the guards that bound an unattended run.

This unit spends a Claude session that merges PRs and deploys them. Four of its lines exist to
defeat failures that produce no error, and each is invisible at deploy time — Ansible reports
the template as applied either way:

1. `flock -n` on ExecStart. Two overlapping runs would fight over the same worktree.
2. `PATH` must name `~/.local/bin`. systemd supplies a minimal PATH exactly as cron does, and
   without it `claude` is not found — the same trap already recorded for the host crons.
3. The arming switch must be wired in BOTH directions. An `enabled: true` with no `false` arm
   is a one-way door: the operator can start the timer and not stop it.
4. The prompt's caps must be the unit's caps. A prompt naming a different PR cap or timeout
   from `defaults/main.yml` is how a session starts a landing it has no budget to finish.

Every template here is asserted on its RENDER. Several of these claims used to be substring
matches on the Jinja itself — the `{% if %}` that gates the Kuma beat, the `{{ ... }}` that
builds its URL, the variable names the prompt is supposed to interpolate — and each of those
holds only while the expression is spelled the way the pattern spells it (#3202). Rendering the
variable to a value no secret holds asserts the same couplings and also catches an expression
that reads the wrong variable.

Run: uv run pytest ansible/tests/setup/test_renovate_agent_unit.py
"""

import json
import re

import pytest
from lib import yaml_fast
from _helpers import ANSIBLE
from _kuma_monitors import entity, monitors_text
from _setup_render import render_setup_text

ROLE_NAME = "renovate_agent"
ROLE = ANSIBLE / "roles" / "setup" / ROLE_NAME
TEMPLATES = ROLE / "templates"
UNIT = "renovate-agent.service.j2"
TIMER = "renovate-agent.timer.j2"
PROMPT = "prompt.txt.j2"
TASKS = ROLE / "tasks" / "service.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

# A value no secret holds, so a render carrying it carries THIS variable's value.
SENTINEL = "renovate-agent-render-sentinel"


def render(template: str, overrides: dict | None = None) -> str:
    """One of this role's templates rendered at inventory values, `overrides` on top."""
    return render_setup_text(ROLE_NAME, template, overrides)


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
    """The unit rendered with the push token armed, so the gated beat line is present."""
    assert (TEMPLATES / UNIT).is_file(), f"{UNIT} is missing — the agent unit is gone"
    return render(UNIT, {TOKEN: SENTINEL})


@pytest.fixture(scope="module")
def defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


def test_execstart_serializes_with_a_nonblocking_lock(unit: str) -> None:
    exec_starts = directive(unit, "ExecStart")
    assert exec_starts, "the unit has no ExecStart"
    assert any("flock -n" in e for e in exec_starts), (
        "ExecStart must take /var/lock/renovate-agent.lock with -n: two overlapping runs "
        "fight over the same worktree, and a daily timer should say so rather than queue"
    )


def test_execstart_runs_the_wrapper_not_claude_directly(unit: str) -> None:
    """The wrapper is what gates the tick, measures the delta and posts the digest."""
    joined = " ".join(directive(unit, "ExecStart"))
    assert "/opt/renovate-agent/renovate_agent.py" in joined


def test_path_carries_the_user_local_bin(unit: str) -> None:
    """`claude` and `uv` live under ~/.local/bin; systemd's default PATH omits it."""
    envs = directive(unit, "Environment")
    paths = [e for e in envs if e.startswith("PATH=")]
    assert paths, "the unit sets no PATH"
    # systemd's last assignment of a variable wins, so the effective PATH is paths[-1].
    assert ".local/bin" in paths[-1]


def test_the_unit_pins_land_sh_to_renovates_prs(unit: str) -> None:
    """The contract's "never a PR by another author" is held by `land.sh --arm-merge`,
    which reads LAND_REQUIRE_AUTHOR. The login must be the one the wrapper's own
    census filters on (`RENOVATE_AUTHOR`), read from its source rather than typed here."""
    # fact: ansible/roles/setup/renovate_agent/CLAUDE.md#Autonomous-role contract (it merges and deploys with no human in the loop)
    envs = directive(unit, "Environment")
    required = [e for e in envs if e.startswith("LAND_REQUIRE_AUTHOR=")]
    assert required, "the unit sets no LAND_REQUIRE_AUTHOR"
    wrapper = (ROLE / "files" / "renovate_agent.py").read_text()
    census = re.search(r'^RENOVATE_AUTHOR = "([^"]+)"', wrapper, re.MULTILINE)
    assert census, "the wrapper's RENOVATE_AUTHOR census filter is gone"
    assert required[-1] == f"LAND_REQUIRE_AUTHOR={census.group(1)}"


def test_onfailure_pages(unit: str) -> None:
    assert "renovate-agent-alert.service" in directive(unit, "OnFailure")


def test_the_unit_holds_no_webhook(unit: str) -> None:
    """`systemctl show -p ExecStart` serves unit content over the system bus to any local user."""
    assert "discord.com/api/webhooks" not in unit
    assert "gitops_deploy_discord_webhook" not in unit


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
    tasks = yaml_fast.safe_load(TASKS.read_text())
    enable = [
        t for t in tasks if t.get("name", "").startswith("Enable and start the timer")
    ]
    assert len(enable) == 1, "expected exactly one arming task"
    systemd = enable[0]["ansible.builtin.systemd"]
    assert "renovate_agent_enabled" in str(systemd["enabled"])
    assert "stopped" in str(systemd["state"]), (
        "state must fall back to stopped when renovate_agent_enabled is false"
    )


def test_the_role_kicks_no_run_on_config_change() -> None:
    """A config edit must not spend a session as a side effect — unlike renovate_notify."""
    text = "".join(p.read_text() for p in sorted((ROLE / "tasks").glob("*.yml")))
    assert "Install agent Python files" in text, (
        "the task-file glob read the wrong files"
    )
    assert "Run renovate-agent once" not in text


def test_the_prompt_quotes_the_caps_it_is_given(defaults: dict) -> None:
    """The prompt's numbers must FOLLOW the defaults, not merely mention their names.

    Each cap is rendered twice — at its default and at a value no default holds — so a number
    typed into the prompt fails here. The old form matched the variable's name in the source,
    which a mention in a comment satisfies just as well.
    """
    sentinels = {
        "renovate_agent_max_prs": 97,
        "renovate_agent_run_timeout_s": 9731,
        "renovate_agent_budget_usd": 97.31,
    }
    at_defaults = render(PROMPT)
    for var, moved_to in sentinels.items():
        assert var in defaults, f"{var} is referenced by the prompt but has no default"
        assert str(defaults[var]) in at_defaults, (
            f"the prompt does not quote {var}'s default {defaults[var]!r}"
        )
        moved = render(PROMPT, {var: moved_to})
        assert str(moved_to) in moved, (
            f"the prompt hardcodes what should come from {var}: moving it to {moved_to} "
            "changes nothing in the rendered prompt"
        )


def test_the_prompt_invokes_the_skill() -> None:
    assert render(PROMPT).lstrip().startswith("/renovate-prs")


def test_the_timer_is_persistent() -> None:
    """A host down at the scheduled minute must still run the tick — the backlog only grows."""
    assert "Persistent=true" in render(TIMER)


# ── the alive beat: the tile, the unit's push, and the deadline that ties them ───────────

ROTATION = ANSIBLE / "secret_rotation.yml"
TOKEN = "renovate_agent_kuma_push_token"
TILE = "renovate-agent-alive.json"
DAY = 86400


def _alive_tile() -> dict:
    """The Renovate Agent tile as it renders with its push token armed.

    `entity` fails naming the tile when the render does not carry it, where the source read it
    replaces had to substitute every `{{ ... }}` to `0` before the parse — so an interval moved
    into a role default read as 0 and the band below compared nothing.
    """
    return entity(TILE, TOKEN)


CONFIG_ENV = "config.env.j2"


def test_the_unit_beats_kuma_only_after_a_clean_run(unit: str) -> None:
    posts = directive(unit, "ExecStartPost")
    assert posts and any("$KUMA_PUSH_URL" in p for p in posts), (
        "ExecStartPost must push the Kuma beat: without it a timer that stops firing is "
        "invisible until someone notices the backlog"
    )
    assert any("/etc/renovate-agent/config.env" in p for p in posts), (
        "the beat must read its URL from the 0600 config.env at run time"
    )
    # The gate, read off two renders rather than off the `{% if %}` line: the push must be
    # there with the token set and gone without it.
    disarmed = directive(render(UNIT, {TOKEN: ""}), "ExecStartPost")
    assert not any("$KUMA_PUSH_URL" in p for p in disarmed), (
        "the beat must be gated on the token, or a checkout without the secret renders a "
        "URL with an empty token and curl -f fails every otherwise-clean run"
    )


def test_the_unit_holds_no_push_token(unit: str) -> None:
    """A unit line is public. `systemctl show <unit> -p ExecStartPost` serves it over the
    system bus to any local user, so a token interpolated into an Exec line is readable
    without sudo — the same reasoning that keeps the alert webhook out of the unit above.
    """
    # The `unit` fixture renders with the token set to SENTINEL, so the VALUE is what to look
    # for: an interpolation anywhere in the unit puts the sentinel into a published line. The
    # source form of this check matched one spelling of the interpolation and nothing else.
    assert SENTINEL not in unit, (
        f"{TOKEN} is interpolated into the unit — move it to config.env and reference "
        "$KUMA_PUSH_URL, as renovate-notify.service.j2 does"
    )
    assert "/api/push/" not in unit, "the push URL must not appear in a unit line"


def test_config_env_carries_the_gated_push_url() -> None:
    """The other half of the move: the URL has to land somewhere 0600, still gated on the
    token so a checkout without the secret renders an empty value rather than a broken URL."""
    armed = render(CONFIG_ENV, {TOKEN: SENTINEL})
    assert "KUMA_PUSH_URL=" in armed, (
        "config.env must carry the push URL the unit reads"
    )
    assert f"/api/push/{SENTINEL}" in armed, (
        f"config.env must build the URL from {TOKEN}; it renders "
        f"{[ln for ln in armed.splitlines() if 'KUMA_PUSH_URL' in ln]}"
    )
    # The line is unconditional and its VALUE is what the gate decides, so the disarmed render
    # must carry an EMPTY assignment rather than no assignment: the unit's own `curl -f` is
    # skipped on an empty variable, where a URL ending in an empty token 404s every run.
    disarmed = [
        ln
        for ln in render(CONFIG_ENV, {TOKEN: ""}).splitlines()
        if "KUMA_PUSH_URL" in ln
    ]
    assert disarmed == ["KUMA_PUSH_URL="], (
        "KUMA_PUSH_URL must be gated on the token, or an unset secret renders a URL ending "
        f"in an empty token and every push 404s. Rendered: {disarmed}"
    )
    tasks = TASKS.read_text()
    assert re.search(
        r"dest: /etc/renovate-agent/config\.env\n\s+owner:.*\n\s+group:.*\n\s+mode: \"0600\"",
        tasks,
    ), (
        "config.env must stay 0600 — it is now the only place the push token lands on disk"
    )


def test_the_tile_and_the_unit_share_one_token() -> None:
    # Rendered at a sentinel this variable alone holds: the tile must carry THAT value, which
    # is a stronger claim than the tile's source naming the variable somewhere.
    tile = json.loads(
        re.search(
            rf"^  {re.escape(TILE)}: \|\n\s+(\{{.*\}})$",
            monitors_text({TOKEN: SENTINEL}),
            re.M,
        ).group(1)
    )
    assert tile["push_token"] == SENTINEL, (
        "the tile must embed the same SOPS var the unit pushes with, or the beat lands on a "
        "monitor that does not exist and the tile sits red"
    )
    assert f"\n  {TOKEN}:\n" in ROTATION.read_text(), (
        f"{TOKEN} is not registered in secret_rotation.yml — run secret_rotation.py sync"
    )


def test_the_deadline_straddles_the_daily_period() -> None:
    """Below one period the tile fires DOWN on a run that merely started late; at two periods
    a whole missed run goes unreported. The timer is daily with a 10-min jitter and up to a
    100-min run, so the beat-to-beat gap can exceed a day by under two hours."""
    interval = _alive_tile()["interval"]
    assert DAY + 2 * 3600 < interval < 2 * DAY, (
        f"interval {interval}s must sit between one jittered daily period and two days"
    )
    defaults = yaml_fast.safe_load(DEFAULTS.read_text())
    assert re.fullmatch(
        r"\*-\*-\* \d\d:\d\d:\d\d( [\w/]+)?", defaults["renovate_agent_oncalendar"]
    ), (
        "the deadline above assumes a once-daily OnCalendar — re-derive it if the cadence moves"
    )


# ── the denylist exclusion: the prompt must leave a `k8s_autodeploy: false` PR to a person ──

RENOVATE = ANSIBLE.parent / "renovate.json"

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


# ── the manual hand-off: the prompt must hand its own superseding PR to a person ──
#
# A finished `manual —` bump ends as a PR the session opened itself, which LAND_REQUIRE_AUTHOR
# refuses to arm. The operator hands that PR off rather than landing it, so the prompt has
# to rule out the `--any-author` override.


def prompt_handoff_problems(prompt: str) -> list[str]:
    """Every way the prompt can fail to hand off its own superseding PR. Empty means it does."""
    problems: list[str] = []
    if "never pass `--any-author`" not in prompt:
        problems.append(
            "the prompt does not forbid --any-author, so the agent can override the author "
            "gate and land the superseding PR it opened"
        )
    if "hand-off finding" not in prompt:
        problems.append(
            "the prompt does not ask for a hand-off finding, so a finished manual bump sits "
            "open with nothing telling a person to land it"
        )
    if _BRANCH not in prompt:
        problems.append(
            "the prompt does not name the hand-off branch, so the wrapper's census of the PRs "
            "the run handed off (agent_logic.handed_off) matches nothing it opened"
        )
    return problems


def test_the_prompt_hands_off_its_own_superseding_pr() -> None:
    # fact: ansible/roles/setup/renovate_agent/CLAUDE.md#Autonomous-role contract (it merges and deploys with no human in the loop)
    problems = prompt_handoff_problems(render(PROMPT))
    assert not problems, "\n".join(problems)


_FORBID = "never pass `--any-author`"
_HANDOFF = "file a hand-off finding"
# The prefix agent_logic.handed_off matches: the run branch (config.env's BRANCH) plus `-`.
# Read from defaults rather than written as the Jinja reference: the prompt is asserted on its
# RENDER, where the reference has become the branch name itself.
_BRANCH = f"`{yaml_fast.safe_load(DEFAULTS.read_text())['renovate_agent_branch']}-<"


def test_a_prompt_forbidding_the_override_and_asking_for_a_handoff_is_clean() -> None:
    assert prompt_handoff_problems(f"{_FORBID}; {_HANDOFF}; {_BRANCH}") == []


@pytest.mark.parametrize(
    ("dropped", "fragment"),
    [
        (_FORBID, "--any-author"),
        (_HANDOFF, "hand-off finding"),
        (_BRANCH, "hand-off branch"),
    ],
)
def test_a_prompt_missing_any_handoff_rule_is_flagged(
    dropped: str, fragment: str
) -> None:
    prompt = "; ".join(p for p in (_FORBID, _HANDOFF, _BRANCH) if p != dropped)
    problems = prompt_handoff_problems(prompt)
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
