"""Each lander unit's LAND_DEADLINE leaves land-wait room for `verify`'s wait on the soak (#5427).

`land_wait` gives up once `promotion.wait_cap` runs out, whatever the soak's own deadline
says. The cap is LAND_DEADLINE less what the landing already spent and `DEADLINE_RESERVE_S`.
Before the wait, a landing spends up to its merge and master CI budgets, the `Options`
defaults both landers run with. The tick has no fixed budget, and the headroom covers it.

The wait needs `verify`'s wait on the soak that the publish job renders from this tree, plus
promote's `render` and `publish` jobs, which run before any promotion can start. The soak's
`timeout` grows by a queueing round with every five Kustomizations, so the PR that hands the
next tags to Flux is the one this turns red.

Run: uv run pytest ansible/tests/setup/test_lander_deadlines_cover_the_soak.py
"""

import re
from types import SimpleNamespace
from typing import cast

import pytest
from _setup_render import render_setup_text
from deploy_tools.bundle_census import published_tags
from deploy_tools.land_lib import handoff, options, promotion
from deploy_tools.land_lib.landing import Landing
from deploy_tools.promote_lib import wait
from lib import yaml_fast
from lib.exit_codes import RENDER_OK
from lib.repo_paths import REPO
from renderer import homelab_render

LANDERS = {
    "claude-land": ("claude_code", "claude-land@.service.j2"),
    "renovate-agent-land": ("renovate_agent", "renovate-agent-land@.service.j2"),
}
# What each unit set before #5427, for the red half against the same render.
BEFORE_5427 = {"claude-land": 5400, "renovate-agent-land": 3300}
# Every value that has to move with LAND_DEADLINE, named in the failure.
CASCADE = (
    "Raise LAND_DEADLINE in both lander units, then what sits above it: claude-land's "
    "TimeoutStartSec and land_lib/handoff.py:START_TIMEOUT_S; renovate-agent-land's "
    "TimeoutStartSec, land_renovate_pr.py:LAND_TIMEOUT_S, and renovate_agent's "
    "bash_timeout_ms, run_timeout_s and unit_timeout"
)


@pytest.fixture(scope="module")
def soak(tmp_path_factory) -> dict:
    """The `soak` Kustomization a placeholder render of the published tags writes."""
    tags = published_tags()
    assert "bento-pdf" in tags, tags
    out = tmp_path_factory.mktemp("published") / "out"
    assert homelab_render.cluster(out, tags, None) == RENDER_OK
    doc = yaml_fast.safe_load((out / "flux" / "soak.yaml").read_text())
    assert doc["metadata"]["name"] == wait.SOAK
    return doc


def publish_pipeline_s() -> int:
    """The longest promote's `render` and `publish` jobs may run, which precede a promotion."""
    jobs = yaml_fast.safe_load(
        (REPO / ".github/workflows/promote.yml").read_text(encoding="utf-8")
    )["jobs"]
    return sum(int(jobs[job]["timeout-minutes"]) * 60 for job in ("render", "publish"))


def deadline(lander: str) -> int:
    role, template = LANDERS[lander]
    text = render_setup_text(role, template)
    (value,) = re.findall(
        rf"^Environment={options.DEADLINE_ENV}=(\d+)$", text, re.MULTILINE
    )
    return int(value)


def shortfall(land_deadline: int, soak: dict) -> float:
    """How far land-wait's cap falls short of what a healthy promotion takes; <= 0 fits."""
    budgets = options.Options(pr="1")
    spent = budgets.merge_timeout + budgets.ci_timeout
    landing = SimpleNamespace(
        opts=SimpleNamespace(deadline=land_deadline),
        tools=SimpleNamespace(clock=lambda: float(spent)),
        ledger=SimpleNamespace(t_start=0.0),
    )
    cap = promotion.wait_cap(cast(Landing, landing))
    assert cap is not None
    verify_wait = wait.soak_deadline_s([soak]) // 2
    assert verify_wait > 0, f"the rendered soak's timeout is unreadable: {soak['spec']}"
    return verify_wait + publish_pipeline_s() - cap


@pytest.mark.parametrize("lander", LANDERS)
def test_the_wait_cap_covers_verifys_wait_on_the_rendered_soak(lander, soak):
    short = shortfall(deadline(lander), soak)
    assert short <= 0, (
        f"{lander}'s land-wait gives up {short:.0f}s before a promotion verify still counts "
        f"as on time, with the soak's timeout at {soak['spec']['timeout']}. {CASCADE}"
    )


@pytest.mark.parametrize("lander", LANDERS)
def test_the_deadlines_before_5427_fall_short_of_the_same_render(lander, soak):
    """The red half: the values the issue measured end a healthy promotion `deferred`."""
    assert shortfall(BEFORE_5427[lander], soak) > 0


def test_the_handoff_outwaits_the_claude_lander():
    """land.sh stops waiting on `systemctl start` only after systemd has stopped the unit."""
    role, template = LANDERS["claude-land"]
    (timeout,) = re.findall(
        r"^TimeoutStartSec=(\d+)h$", render_setup_text(role, template), re.MULTILINE
    )
    assert handoff.START_TIMEOUT_S > int(timeout) * 3600
