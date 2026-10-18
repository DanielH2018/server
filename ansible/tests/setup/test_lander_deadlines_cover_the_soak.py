"""Each lander unit's LAND_DEADLINE leaves land-wait room for `verify`'s wait on the soak (#5427).

`land_wait` gives up once `promotion.wait_cap` runs out, whatever the soak's own deadline
says. The cap is LAND_DEADLINE less what the landing already spent and `DEADLINE_RESERVE_S`.
Before the wait, a landing spends up to its merge and master CI budgets, the `Options`
defaults both landers run with. The tick has no fixed budget, and the headroom covers it.

The wait needs `verify`'s wait on the soak that the publish job renders from this tree, plus
promote's `render`, `images` and `publish` jobs, which run before any promotion can start.
Published tags build images since D5.14 handed ical-proxy and homelab-mcp to Flux, so a
flux-tag landing's own commit can rebuild one, and `publish` needs `images`. The sum counts
each of the three jobs at its `timeout-minutes`, so `images`' 20 minutes bound the landing's
own rebuild. `test_the_pipeline_counts_every_job_publish_waits_on` derives the jobs from
`publish`'s `needs` in the workflow, so a job added ahead of `publish` turns it red.

Another commit's rebuild can still delay the landing's `images` job (#5431). The `images`
concurrency group runs one job and keeps one pending, and a newer pending job evicts the
older. So one rebuild can run ahead of the landing, and the wait counts it as FULL_REBUILD_S,
one measured rebuild of every built image. A second rebuild is inside the landing's own job:
that job builds every content tag GHCR does not serve in its own render, so an image change
whose pending job it evicted is built there, and the job's timeout already counts it.
`tag` and the `verify` job are not on the path: `land-wait` settles on Flux's `flux/soak`
status at R, not on either job. The soak's
`timeout` grows by a queueing round with every five equal Kustomizations, or by a fifth of an
added one's `timeout` once Graham's bound is the smaller (`chained_timeout.queueing_s`), so the
PR that hands the next tags to Flux is the one this turns red.

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
from renderer.renderer_lib import images

LANDERS = {
    "claude-land": ("claude_code", "claude-land@.service.j2"),
    "renovate-agent-land": ("renovate_agent", "renovate-agent-land@.service.j2"),
}
# What each unit set before D5.14 counted `images` (#5466), for the red half against the same
# render. #5427 had raised them from 5400 and 3300.
BEFORE_IMAGES = {"claude-land": 10800, "renovate-agent-land": 10800}
# What each unit set before D5.16 handed code-server to Flux with a 3600s rollout budget (#5512).
BEFORE_CODE_SERVER = {"claude-land": 18000, "renovate-agent-land": 18000}
# The promote jobs a promotion waits on, each counted at its `timeout-minutes`.
PIPELINE = ("render", "images", "publish")
# One `images` job that rebuilds every built image, measured on 2026-10-17 (#5431). Run
# 38992066819's job 117276493018 built all ten in 368s, but n8n-runners failed there after 19s
# (01:49:13 to 01:49:32). Its successful build in run 39036029019 took 45s (13:19:12 to
# 13:19:57), so 368 - 19 + 45. code-server was the longest, at 213s.
FULL_REBUILD_S = 394
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


def _promote_jobs() -> dict:
    return yaml_fast.safe_load(
        (REPO / ".github/workflows/promote.yml").read_text(encoding="utf-8")
    )["jobs"]


def publish_pipeline_s(pipeline: tuple[str, ...] = PIPELINE) -> int:
    """The longest the promote jobs that precede a promotion may run, `images` included."""
    jobs = _promote_jobs()
    return sum(int(jobs[job]["timeout-minutes"]) * 60 for job in pipeline)


def uncounted(pipeline: tuple[str, ...]) -> set[str]:
    """The jobs `publish` waits on, itself included, that `pipeline` leaves out of the sum."""
    jobs = _promote_jobs()
    path: set[str] = set()
    todo = ["publish"]
    while todo:
        job = todo.pop()
        if job not in path:
            path.add(job)
            needs = jobs[job].get("needs", [])
            todo.extend([needs] if isinstance(needs, str) else needs)
    return path - set(pipeline)


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
    return verify_wait + publish_pipeline_s() + FULL_REBUILD_S - cap


@pytest.mark.parametrize("lander", LANDERS)
def test_the_wait_cap_covers_verifys_wait_on_the_rendered_soak(lander, soak):
    short = shortfall(deadline(lander), soak)
    assert short <= 0, (
        f"{lander}'s land-wait gives up {short:.0f}s before a promotion verify still counts "
        f"as on time, with the soak's timeout at {soak['spec']['timeout']}. {CASCADE}"
    )


@pytest.mark.parametrize("lander", LANDERS)
def test_the_deadlines_before_images_counted_fall_short_of_the_same_render(
    lander, soak
):
    """The red half: the values that left `images` out end a healthy promotion `deferred`."""
    assert shortfall(BEFORE_IMAGES[lander], soak) > 0


@pytest.mark.parametrize("lander", LANDERS)
def test_the_deadlines_before_code_server_fall_short_of_the_same_render(lander, soak):
    """The red half: code-server's 3600s `timeout` on the chain outgrew 18000's headroom."""
    assert "code-server" in published_tags()
    assert shortfall(BEFORE_CODE_SERVER[lander], soak) > 0


def test_the_handoff_outwaits_the_claude_lander():
    """land.sh stops waiting on `systemctl start` only after systemd has stopped the unit."""
    role, template = LANDERS["claude-land"]
    (timeout,) = re.findall(
        r"^TimeoutStartSec=(\d+)h$", render_setup_text(role, template), re.MULTILINE
    )
    assert handoff.START_TIMEOUT_S > int(timeout) * 3600


def test_published_tags_build_images():
    """ical-proxy and homelab-mcp build images and are flux-owned since D5.14 (#5466)."""
    builders = set(images.builder_roles())
    assert "code-server" in builders, builders
    assert {"homelab-mcp", "ical-proxy"} <= builders & set(published_tags()), (
        "the named builders are no longer published tags, so check whether `images` still "
        "belongs in PIPELINE"
    )


def test_the_pipeline_counts_every_job_publish_waits_on():
    missing = uncounted(PIPELINE)
    assert not missing, (
        f"`publish` waits on {sorted(missing)}, which publish_pipeline_s leaves out of the "
        f"landing's wait. {CASCADE}"
    )


def test_a_pipeline_without_images_leaves_it_out():
    """The red half: the sum before D5.14, `render` and `publish` alone, misses `images`."""
    assert uncounted(("render", "publish")) == {"images"}
