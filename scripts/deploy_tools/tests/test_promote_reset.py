"""The `reset_to_last_good` dispatch: `promote.py reset-check`'s refusals and `reset-move`'s write.

`reset-check` runs against `World`'s fake forge. `reset-move` checks out nothing, so its rule
lives in the step text of `.github/workflows/promote.yml`, and each write test runs that step
under bash with fake `curl`, `flux`, `docker` and `gh` that log their argv. The plan is
docs/target-architecture/deploy/d5-bulk-move.md, *A reset input*.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_reset.py
"""

import dataclasses
import re
import shutil
from datetime import UTC, datetime

import pytest
from _promote_fakes import World, digest_of
from lib import yaml_fast
from lib.exit_codes import FAILED, OK
from lib.proc_testing import fake_bin
from lib.proc_testing import run as proc_run
from lib.repo_paths import REPO

from chained_timeout import VERIFY_CAP_S
from deploy_tools.promote_lib import reboot_window, reset

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"
REPO_URL = "oci://ghcr.io/danielh2018/homelab-manifests"
OWN_RUN = "4242"
SUNDAY_0700 = datetime(2026, 10, 18, 7, tzinfo=UTC)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _verified(world) -> tuple[str, str]:
    """`last-good` on c1 and `prod` on T, the tip, as after `tag` moved it; returns (c1, T)."""
    _, c1, _, t = world.commits
    world.publish(c1)
    world.publish(t)
    world.set_last_good(c1)
    world.set_prod(t)
    return c1, t


def _check(world) -> reset.Check:
    return reset.check(reset.live_reset_reads(world.clients()), OWN_RUN)


# -- the hold ------------------------------------------------------------------------------


def test_the_reset_moves_prod_inside_the_reboot_window_under_a_settled_hold(world):
    """`reset-check` takes no clock and no schedule, so the window cannot stop it.

    The plan names a hand-set hold such as `drill` here, but `drill` never names `prod`'s
    commit, so with `prod` ahead it refuses (the next test) and with `prod` on `last-good` it
    moves nothing. The settled stateful hold is the one that moves `prod`.
    """
    c1, t = _verified(world)
    world.run.hold = f"{t} stateful=bento-pdf"
    inside = reboot_window.window(
        reboot_window.read_schedule(world.repo),
        VERIFY_CAP_S,
        SUNDAY_0700,
    )
    assert inside is not None, "the fixture instant is inside the window"
    assert {f.name for f in dataclasses.fields(reset.ResetReads)} == {
        "hold",
        "digest",
        "prod_commit",
        "in_progress",
    }
    assert _check(world) == reset.Check(
        "move", _check(world).reason, digest_of(t), digest_of(c1)
    )


@pytest.mark.parametrize(
    ("hold", "fails", "named"),
    [
        ("none", False, "reads 'none'"),
        ("", False, "reads ''"),
        (None, False, "does not exist"),
        ("drill", True, "could not be read"),
    ],
    ids=["none", "empty", "missing", "a failed read"],
)
def test_the_reset_refuses_without_a_hold(world, hold, fails, named):
    """The red half is a guard that refuses only on an empty value: `none` passes it."""
    _verified(world)
    world.run.hold, world.run.hold_fails = hold, fails
    result = _check(world)
    assert result.verdict == "refused"
    assert named in result.reason


# -- a verification in flight --------------------------------------------------------------


def test_the_reset_refuses_while_a_verification_is_in_flight(world):
    """The red half is a guard that checks only that a hold exists: `drill` passes it."""
    _, t = _verified(world)
    world.run.hold = "drill"
    world.transport.runs = [
        {"id": 7, "head_sha": t},
        {"id": int(OWN_RUN), "head_sha": world.commits[0]},
    ]
    result = _check(world)
    assert result.verdict == "refused"
    assert f"prod's commit {t}" in result.reason
    assert f"run 7 at {t[:8]}" in result.reason
    assert f"run {OWN_RUN}" not in result.reason
    assert "gh run cancel" in result.reason


def test_the_reset_proceeds_on_a_settled_hold_and_compares_digests(world):
    """The red half is a check that matches the SHA string alone: the second fixture."""
    _, t = _verified(world)
    world.run.hold = f"{t} stateful=bento-pdf"
    assert _check(world).verdict == "move"

    # The hold still names T, but `prod` names another digest than `sha-<T>`'s.
    world.transport.tags["prod"] = digest_of("moved-on")
    result = _check(world)
    assert result.verdict == "refused"
    assert "in flight" in result.reason


def test_the_check_exits_0_with_a_move_and_1_with_a_refusal(world, tmp_path, capsys):
    c1, t = _verified(world)
    world.run.hold = f"{t} unverified"
    output = tmp_path / "out"
    assert reset.report(_check(world), str(output)) == OK
    assert output.read_text() == (
        f"move=true\nprod={digest_of(t)}\nlast_good={digest_of(c1)}\n"
    )
    world.run.hold = "none"
    assert reset.report(_check(world), None) == FAILED


# -- nothing to move -----------------------------------------------------------------------


def test_the_reset_moves_nothing_when_prod_already_names_last_good(world):
    c1, _ = _verified(world)
    world.set_prod(c1)
    world.run.hold = "drill"
    result = _check(world)
    assert (result.verdict, result.prod, result.last_good) == (
        "none",
        digest_of(c1),
        digest_of(c1),
    )


# -- reset-move: the compare-and-move ------------------------------------------------------

PROD = digest_of("t")
LAST_GOOD = digest_of("c1")
TAGGED = digest_of("u")

_FAKE = """#!/usr/bin/env bash
[ "{tool}" = docker ] && cat > /dev/null
echo "{tool} $*" >> "$CALLS"
"""

# GHCR for the move step: a pull token, then `prod`'s HEAD, answering FAKE_PROD_DIGEST.
FAKE_CURL = r"""#!/usr/bin/env bash
cat > /dev/null
out= url=
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w|-H|-K|--retry) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
case "$url" in
  *"/token?"*) printf '{"token":"t"}' ;;
  *"/manifests/prod")
    printf 'HTTP/2 200\r\nDocker-Content-Digest: %s\r\n\r\n' "$FAKE_PROD_DIGEST" > "$out"
    printf 200 ;;
  *) echo "fake curl: unexpected $url" >&2; exit 2 ;;
esac
"""


@pytest.fixture(scope="module")
def move_script() -> str:
    jobs = yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    steps = jobs["reset-move"]["steps"]
    return next(s for s in steps if s.get("name") == "Move prod to last-good")["run"]


def _move(tmp_path, script: str, live: str):
    assert shutil.which("bash")
    calls = tmp_path / "calls"
    calls.write_text("")
    stub = fake_bin(
        tmp_path / "bin",
        flux=_FAKE.format(tool="flux"),
        docker=_FAKE.format(tool="docker"),
        curl=FAKE_CURL,
    )
    env = {
        "CALLS": str(calls),
        "PROD": PROD,
        "LAST_GOOD": LAST_GOOD,
        "FAKE_PROD_DIGEST": live,
        "GH_ACTOR": "actor",
        "GITHUB_TOKEN": "job-token",
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "RUNNER_TEMP": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    result = proc_run(["bash", "-c", script], env=env, stub_bin=stub)
    moves = [line for line in calls.read_text().splitlines() if line.startswith("flux")]
    return result, moves


def test_the_write_moves_prod_while_it_names_the_digest_the_read_job_read(
    tmp_path, move_script
):
    result, moves = _move(tmp_path, move_script, PROD)
    assert result.returncode == 0, result.stderr
    assert moves == [f"flux tag artifact {REPO_URL}@{LAST_GOOD} --tag prod"]
    assert (tmp_path / "github_output").read_text() == "moved=true\n"


def test_the_write_moves_nothing_when_prod_already_names_last_good(
    tmp_path, move_script
):
    result, moves = _move(tmp_path, move_script, LAST_GOOD)
    assert result.returncode == 0, result.stderr
    assert moves == []
    assert "already names last-good" in result.stdout


def test_the_write_moves_nothing_when_prod_changed_after_the_read(
    tmp_path, move_script
):
    """The red half is a write that skips the compare: it moves `prod` off TAGGED."""
    result, moves = _move(tmp_path, move_script, TAGGED)
    assert result.returncode == 1
    assert moves == []
    assert PROD in result.stdout and TAGGED in result.stdout

    no_compare = re.sub(
        r'^if \[ "\$live" != "\$PROD" \]; then\n.*?\nfi\n',
        "",
        move_script,
        flags=re.S | re.M,
    )
    assert no_compare != move_script
    (tmp_path / "red").mkdir()
    _, unguarded = _move(tmp_path / "red", no_compare, TAGGED)
    assert unguarded == [f"flux tag artifact {REPO_URL}@{LAST_GOOD} --tag prod"]
