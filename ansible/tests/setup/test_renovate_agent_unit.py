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

Run: uv run pytest ansible/tests/setup/test_renovate_agent_unit.py
"""

import re

import pytest
from lib import yaml_fast
from _helpers import ANSIBLE

ROLE = ANSIBLE / "roles" / "setup" / "renovate_agent"
UNIT = ROLE / "templates" / "renovate-agent.service.j2"
TIMER = ROLE / "templates" / "renovate-agent.timer.j2"
PROMPT = ROLE / "templates" / "prompt.txt.j2"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"


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
    assert UNIT.exists(), f"{UNIT} is missing — the agent unit is gone"
    return UNIT.read_text()


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
    text = TASKS.read_text()
    assert "Run renovate-agent once" not in text


def test_the_prompt_quotes_the_caps_it_is_given(defaults: dict) -> None:
    """The prompt's numbers are Jinja references, so they cannot drift from defaults."""
    prompt = PROMPT.read_text()
    for var in (
        "renovate_agent_max_prs",
        "renovate_agent_run_timeout_s",
        "renovate_agent_budget_usd",
    ):
        assert var in prompt, f"the prompt hardcodes what should come from {var}"
        assert var in defaults, f"{var} is referenced by the prompt but has no default"


def test_the_prompt_invokes_the_skill() -> None:
    assert PROMPT.read_text().lstrip().startswith("/renovate-prs")


def test_the_timer_is_persistent() -> None:
    """A host down at the scheduled minute must still run the tick — the backlog only grows."""
    assert "Persistent=true" in TIMER.read_text()


# ── the alive beat: the tile, the unit's push, and the deadline that ties them ───────────

MONITORS = (
    ANSIBLE / "roles" / "k8s" / "uptime-kuma" / "templates" / "static-monitors.yaml.j2"
)
ROTATION = ANSIBLE / "secret_rotation.yml"
TOKEN = "renovate_agent_kuma_push_token"
DAY = 86400


def _alive_tile() -> dict:
    """The rendered Renovate Agent tile, read out of the template by filename key."""
    m = re.search(
        r"^  renovate-agent-alive\.json: \|\n\s+(\{.*\})$", MONITORS.read_text(), re.M
    )
    assert m, "renovate-agent-alive.json is missing from static-monitors.yaml.j2"
    return __import__("json").loads(re.sub(r"\{\{[^}]*\}\}", "0", m.group(1)))


def test_the_unit_beats_kuma_only_after_a_clean_run(unit: str) -> None:
    posts = directive(unit, "ExecStartPost")
    assert posts and any(f"/api/push/{{{{ {TOKEN} }}}}" in p for p in posts), (
        "ExecStartPost must push the Kuma beat with renovate_agent_kuma_push_token: without "
        "it a timer that stops firing is invisible until someone notices the backlog"
    )
    assert f"{{% if {TOKEN} | default('') %}}" in unit, (
        "the beat must be gated on the token, or a checkout without the secret renders a "
        "URL with an empty token and curl -f fails every otherwise-clean run"
    )


def test_the_tile_and_the_unit_share_one_token(unit: str) -> None:
    tile = re.search(
        r"^  renovate-agent-alive\.json: \|\n\s+(\{.*\})$", MONITORS.read_text(), re.M
    )
    assert tile, "renovate-agent-alive.json is missing from static-monitors.yaml.j2"
    assert f'"push_token": "{{{{ {TOKEN} }}}}"' in tile.group(1), (
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
