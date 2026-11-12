"""`land.sh --fresh` lands from a checkout of origin/master it makes and removes (#7933).

Run: uv run pytest scripts/deploy_tools/tests/test_land_fresh.py
"""

import dataclasses
import subprocess
from pathlib import Path

import pytest

from _land_fakes import Fakes, build_tools
from deploy_tools.land_lib import fresh, handoff
from deploy_tools.land_lib.options import Options
from lib import git_testing
from lib.git_testing import commit, init_repo


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    """A clone whose origin/master moved after the clone, so only a fetch reaches the tip."""
    upstream = init_repo(tmp_path / "upstream")
    commit(upstream, "old", **{"reader.py": "v1"})
    git_testing.git(tmp_path, "clone", "-q", str(upstream), str(tmp_path / "primary"))
    commit(upstream, "move the layout", **{"reader.py": "v2"})
    return tmp_path / "primary"


def _child(seen: list):
    def run(argv, check):
        checkout = Path(argv[0]).parents[2]
        seen.append((argv, (checkout / "reader.py").read_text(), checkout))
        return subprocess.CompletedProcess(argv, 75)

    return run


def test_a_fresh_landing_runs_land_sh_from_a_checkout_of_the_fetched_tip(primary):
    seen: list = []
    rc = fresh.land_fresh(primary, ["--pr", "7"], run=_child(seen))
    ((argv, reader, checkout),) = seen
    assert rc == 75
    assert argv[1:] == ["--pr", "7"] and argv[0].endswith(fresh.LAND_SH)
    assert reader == "v2"
    assert not checkout.exists()
    listed = git_testing.git(primary, "worktree", "list").stdout
    assert str(checkout) not in listed


def test_a_checkout_git_could_not_make_lands_nothing(tmp_path, capsys):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    seen: list = []
    rc = fresh.land_fresh(not_a_repo, ["--pr", "7"], run=_child(seen))
    assert rc == 1 and seen == []
    assert "nothing was landed" in capsys.readouterr().err


def test_the_child_argv_drops_fresh_and_detach_and_keeps_the_resolved_since():
    got = fresh.child_argv(["--pr", "7", "--fresh", "--detach", "--arm-merge"], "abc")
    assert got == ["--pr", "7", "--arm-merge", "--since", "abc"]


def test_the_child_argv_keeps_a_since_the_caller_gave():
    got = fresh.child_argv(["--pr", "7", "--since", "pre", "--fresh"], "abc")
    assert got == ["--pr", "7", "--since", "pre"]


def test_land_hands_a_fresh_landing_to_the_checkout_and_annotates_nothing():
    import land

    tools, calls = build_tools(Fakes())
    handed: list = []

    def fresh_landing(primary, argv):
        handed.append(argv)
        return 75

    tools = dataclasses.replace(tools, fresh_landing=fresh_landing)
    rc = land.main(
        ["--pr", "999", "--arm-merge", "--await-merge", "--fresh"], tools=tools
    )
    assert rc == 75
    assert handed == [["--pr", "999", "--arm-merge", "--await-merge"]]
    assert not [c for c in calls if c[0] in ("logger", "gh")]


def test_the_lander_unit_refuses_fresh():
    assert "--fresh" in handoff.refused_flags(Options(pr="7", fresh=True))


def test_the_lander_unit_accepts_a_landing_without_fresh():
    assert handoff.refused_flags(Options(pr="7")) == []
