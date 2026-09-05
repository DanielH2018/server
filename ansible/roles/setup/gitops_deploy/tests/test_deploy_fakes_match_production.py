"""Where `_deploy_fakes` answers a boundary, it must answer it the way production does.

A fake that is stricter than production is not a safe fake. It reads green forever — every
test that would have caught the divergence fails to be written, because the fake refuses the
input production accepts. That is the "green while checking nothing" class from the repo's
testing rules, arriving through the test double rather than through the check.

Two seams had drifted, and each test below is a pair: an input the aligned fake must ACCEPT
and one it must REJECT, so a fake that went permissive everywhere fails as loudly as one that
went strict everywhere.

`service_healthy` is checked DIFFERENTIALLY — the fake and `deploy_io.service_healthy` are
both called on the same scripted checkout, so this file pins agreement rather than restating
the fake's own docstring. Only the unrendered case can be driven that way: production's
rendered path calls `health_ok`, which shells out to `docker inspect`.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_fakes_match_production.py
"""

import pathlib

import pytest

import deploy_io
from _deploy_fakes import LOCAL, ORIGIN, ScriptedTick

GATE_TIMEOUT_S = 1.0
# The `git diff -U0` argv `deploy_io.k8s_image_diff` builds, which is what the fake parses the
# service name out of. Built here from the same shape rather than hand-written, so a change to
# the path layout breaks this file instead of silently missing the branch.
DIFF_ARGV = [
    "git",
    "diff",
    "-U0",
    f"{LOCAL}..{ORIGIN}",
    "--",
    "ansible/roles/k8s/sonarr/defaults/main.yml",
]


@pytest.fixture
def tick(tmp_path: pathlib.Path) -> ScriptedTick:
    return ScriptedTick(tmp_path)


def test_an_unrendered_service_is_vacuously_healthy_in_both_the_fake_and_production(
    tick: ScriptedTick,
):
    """The dozzle case: a service in `containers_list` that renders no compose on THIS host.

    `containers_for` finds no compose, so production gates an empty container list and
    `all([])` is True — the service passes without a container ever being polled. The fake
    used to raise on exactly this input, which is what made the vacuous path untestable.

    Both sides are called on the same directory, so this fails if either one moves.
    """
    repo = str(tick.repo)
    assert deploy_io.service_healthy(repo, "dozzle", GATE_TIMEOUT_S) is True
    assert tick.service_healthy(repo, "dozzle", GATE_TIMEOUT_S) is True


def test_scripting_an_unrendered_service_unhealthy_is_rejected(tick: ScriptedTick):
    """The reject half. The OLD fake answered False here, which production cannot report.

    A test leaning on that answer would assert the deployer's behaviour against a state no
    tick ever sees: with no rendered compose there is no container to be unhealthy.
    """
    tick.healthy["dozzle"] = False
    with pytest.raises(AssertionError, match="never rendered its compose"):
        tick.service_healthy(str(tick.repo), "dozzle", GATE_TIMEOUT_S)


def test_a_rendered_service_with_no_scripted_answer_is_still_rejected(
    tick: ScriptedTick,
):
    """Loosening the unrendered case must not loosen the rendered one.

    `render()` seeds the service healthy, so reaching this needs the entry removed by hand —
    which is the shape of a test that renders one service and gates another.
    """
    tick.render("sonarr")
    del tick.healthy["sonarr"]
    with pytest.raises(AssertionError, match="which no test scripted"):
        tick.service_healthy(str(tick.repo), "sonarr", GATE_TIMEOUT_S)


def test_an_unscripted_image_diff_is_rejected(tick: ScriptedTick):
    """The reject half for the one `run()` path that used to default instead of raising.

    `self.diffs.get(key, "")` handed back an empty diff for a service nobody scripted, and an
    empty diff is a real production answer meaning "the range touched no line of that file".
    A test that forgot to script one therefore read as a clean no-op and passed.
    """
    with pytest.raises(AssertionError, match="no test scripted"):
        tick.run(DIFF_ARGV)


def test_a_scripted_empty_image_diff_is_still_answered(tick: ScriptedTick):
    """The accept half: an empty diff is legitimate, so long as the test says so on purpose."""
    tick.diffs["sonarr"] = ""
    assert tick.run(DIFF_ARGV) == ""
