"""`reconcile`'s hold for a lost run that left no verdict status naming its form (#7876).

`settle` holds a T that changes a snapshot role or a forward-only tag without a reset, and
`reconcile` must write the same hold when the lost run's `homelab/promote-verdict` status is
missing or names another digest. Each test runs `promote.decide` on the #7863 lost-settle tags
over the fake forge, carries its step outputs through `promote.yml`'s job outputs into the
`reconcile` step's env, and runs that step under the fakes of `test_promote_reconcile.py`.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_reconcile_lost_form.py
"""

import json
import re

import pytest
from lib import yaml_fast
from _promote_fakes import World, digest_of
from test_promote_decide_lost_settle import Clock, _reads
from test_promote_forward_only import FLAGGED, LONGHORN
from test_promote_reconcile import CONTEXT, _run
from test_promote_settle import REPO_URL, WORKFLOW, _step
from test_promote_stateful_hold import SONARR_SNAP, _bundle

import promote

STEP = "Apply the lost run's verdict"
NEEDS = re.compile(r"^\$\{\{\s*needs\.promote\.outputs\.(\w+)\s*\}\}$")
STEPS = re.compile(r"^\$\{\{\s*steps\.decide\.outputs\.(\w+)\s*\}\}$")


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _lost(world, monkeypatch, last_good: str, prod: str, files) -> dict[str, str]:
    """Run `decide` with `prod`, holding `files`, on a run that lost its settle.

    Returns the step outputs `decide` writes.
    """
    world.set_last_good(last_good)
    world.set_prod(prod, files=files)
    world.transport.tags["superseding"] = digest_of(prod)
    tip = world.commit("tip")
    world.publish(tip)
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    world.transport.runs = [{"id": 9, "head_sha": tip}]
    decision = promote.decide(_reads(world, Clock()), supersede_on=True)
    assert decision.lost_settle == prod, decision.reason
    output = world.tmp_path / "github_output"
    output.write_text("")
    promote.report(tip, decision, str(output))
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def _reconcile_env(jobs, step_outputs: dict[str, str]) -> dict[str, str]:
    """The `reconcile` step's env, each `needs.promote.outputs` value resolved from `decide`."""
    job_outputs = {}
    for name, expr in jobs["promote"]["outputs"].items():
        if found := STEPS.match(expr):
            job_outputs[name] = step_outputs.get(found.group(1), "")
    env = {}
    for key, expr in _step(jobs["reconcile"], STEP)["env"].items():
        if found := NEEDS.match(str(expr)):
            env[key] = job_outputs.get(found.group(1), "")
    return env


def _reconcile(tmp_path, jobs, outputs: dict[str, str], statuses: list[dict]):
    env = _reconcile_env(jobs, outputs)
    script = _step(jobs["reconcile"], STEP)["run"]
    result, calls = _run(
        tmp_path,
        script,
        FAKE_PROD_DIGEST=outputs["lost_settle_digest"],
        FAKE_STATUSES=json.dumps(statuses),
        **env,
    )
    return result, [c for c in calls if not c.startswith("gh api")]


def _snapshot_world(tmp_path, monkeypatch) -> tuple[dict[str, str], str]:
    """Lose the settle of a `prod` that changes snapshot role sonarr's own digest."""
    world = World(_subdir(tmp_path))
    c0, c1 = world.commits[0], world.commits[1]
    world.store(digest_of(c0), _bundle({"sonarr": "s1", "traefik": "t1"}))
    files = _bundle({"sonarr": "s2", "traefik": "t1"}, snap=SONARR_SNAP)
    return _lost(world, monkeypatch, c0, c1, files), c1


def _forward_only_world(tmp_path, monkeypatch) -> tuple[dict[str, str], str]:
    """Lose the settle of a `prod` that changes the flagged longhorn role's digest."""
    world = World(_subdir(tmp_path), size=1)
    base = world.commit_files("base", {LONGHORN: FLAGGED})
    bump = world.commit("bump longhorn")
    world.store(digest_of(base), _bundle({"longhorn": "l1"}))
    return _lost(world, monkeypatch, base, bump, _bundle({"longhorn": "l2"})), bump


def _subdir(tmp_path):
    """A directory for the world's repo, apart from the reconcile fakes in `tmp_path`."""
    (tmp_path / "w").mkdir()
    return tmp_path / "w"


def _hold(body: str) -> str:
    return f"gh variable set DEPLOY_HOLD --body {body} -R DanielH2018/server"


def test_a_missing_status_for_a_snapshot_role_change_holds_stateful_and_tags_held(
    tmp_path, jobs, monkeypatch
):
    """The Verify-by case: a failure (here, no) status and a prod run that changed sonarr."""
    outputs, c1 = _snapshot_world(tmp_path, monkeypatch)
    result, calls = _reconcile(tmp_path, jobs, outputs, statuses=[])
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        _hold(f"{c1} stateful=unverified"),
        f"flux tag artifact {REPO_URL}@{digest_of(c1)} --tag held",
    ]


def test_a_status_for_another_digest_still_holds_a_snapshot_role_change_stateful(
    tmp_path, jobs, monkeypatch
):
    outputs, c1 = _snapshot_world(tmp_path, monkeypatch)
    other = "sha256:" + "f" * 64
    status = {
        "context": CONTEXT,
        "state": "failure",
        "description": f"failed {other} reset",
    }
    result, calls = _reconcile(tmp_path, jobs, outputs, statuses=[status])
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        _hold(f"{c1} stateful=unverified"),
        f"flux tag artifact {REPO_URL}@{digest_of(c1)} --tag held",
    ]


def test_a_missing_status_for_a_forward_only_change_holds_on_the_commit_and_tags_held(
    tmp_path, jobs, monkeypatch
):
    outputs, bump = _forward_only_world(tmp_path, monkeypatch)
    result, calls = _reconcile(tmp_path, jobs, outputs, statuses=[])
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        _hold(bump),
        f"flux tag artifact {REPO_URL}@{digest_of(bump)} --tag held",
    ]
