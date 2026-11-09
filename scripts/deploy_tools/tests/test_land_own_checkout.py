"""land.sh refuses to land from a checkout whose landing code master has changed (#7820).

Run: uv run pytest scripts/deploy_tools/tests/test_land_own_checkout.py
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _land_fakes import PRIMARY, Fakes, make_landing
from deploy_tools.land_lib import classify, own_checkout
from deploy_tools.land_lib.outcome import Outcome
from lib import git_testing
from lib.git_testing import commit, init_repo


def _git(*args, cwd, check):
    return git_testing.git(cwd, *args, check=check)


@pytest.fixture
def history(tmp_path: Path) -> dict[str, str]:
    """A master where `reader.py` changed after `old`, then a PR squash-merged as `merge`."""
    repo = init_repo(tmp_path / "repo")
    old = commit(repo, "old", **{"reader.py": "v1", "other.py": "v1"})
    commit(repo, "move the layout", **{"reader.py": "v2", "other.py": "v2"})
    merge = commit(repo, "the PR", **{"pr.txt": "x"})
    return {"repo": str(repo), "old": old, "merge": merge}


def _checkout(history: dict[str, str], ref: str) -> Path:
    repo = Path(history["repo"])
    git_testing.git(repo, "checkout", "-q", "--detach", ref)
    return repo


def test_a_checkout_missing_a_master_change_to_its_code_is_flagged(history):
    root = _checkout(history, history["old"])
    base = f"{history['merge']}^1"
    assert own_checkout.stale_paths(_git, root, base, ["reader.py"]) == ["reader.py"]


def test_a_checkout_at_the_merge_is_clean(history):
    root = _checkout(history, history["merge"])
    base = f"{history['merge']}^1"
    assert own_checkout.stale_paths(_git, root, base, ["reader.py"]) == []


def test_a_branch_carrying_masters_content_under_other_commits_is_clean(history):
    """A squash merge leaves the branch without master's commit but with its content."""
    root = _checkout(history, history["old"])
    commit(root, "same change, own commit", **{"reader.py": "v2"})
    base = f"{history['merge']}^1"
    assert own_checkout.stale_paths(_git, root, base, ["reader.py"]) == []


def test_a_path_the_landing_did_not_load_is_not_compared(history):
    root = _checkout(history, history["old"])
    base = f"{history['merge']}^1"
    assert own_checkout.stale_paths(_git, root, base, ["pr.txt"]) == []


def test_an_unreadable_base_is_unknown_rather_than_clean(history):
    root = _checkout(history, history["old"])
    assert own_checkout.stale_paths(_git, root, "0" * 40, ["reader.py"]) is None


def test_loaded_paths_keeps_only_modules_under_the_checkout(tmp_path):
    inside = SimpleNamespace(__file__=str(tmp_path / "scripts" / "a.py"))
    outside = SimpleNamespace(__file__="/usr/lib/python3/json/__init__.py")
    builtin = SimpleNamespace()
    got = own_checkout.loaded_paths(tmp_path, [inside, outside, builtin])
    assert got == ["scripts/a.py"]


# The readers #7820's stale checkout misread, or that read the layout beside them.
_INCIDENT_READERS = frozenset(
    {
        "scripts/lib/render_guard.py",
        "scripts/deploy_tools/deploy_lib/owners.py",
        "scripts/lib/k8s_roles.py",
        "scripts/lib/ansible_inventory.py",
    }
)

_LOADED_AFTER_IMPORT = """
import sys
sys.path[:0] = ["scripts", "scripts/deploy_tools"]
import land
from deploy_tools.land_lib import own_checkout
print("\\n".join(own_checkout.loaded_paths(own_checkout.OWN_CHECKOUT, list(sys.modules.values()))))
"""


def test_importing_land_loads_the_incident_readers():
    """A fresh interpreter: this worker's own imports would fill the census for it."""
    root = own_checkout.OWN_CHECKOUT
    run = subprocess.run(
        [sys.executable, "-c", _LOADED_AFTER_IMPORT],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    missing = _INCIDENT_READERS - set(run.stdout.split())
    assert not missing, f"no longer loaded when land.py starts: {sorted(missing)}"


def test_a_current_checkout_lets_the_landing_continue():
    ln, _ = make_landing(Fakes(stale_code=[]))
    ln.merge_sha = "m" * 40
    classify.refuse_stale_code(ln)


def test_a_stale_checkout_names_the_file_and_a_checkout_at_the_merge():
    ln, _ = make_landing(Fakes(stale_code=["scripts/lib/render_guard.py"]), since="pre")
    ln.merge_sha = "m" * 40
    with pytest.raises(Outcome) as raised:
        classify.refuse_stale_code(ln)
    msg = raised.value.detail
    assert raised.value.rc == 1 and "scripts/lib/render_guard.py" in msg
    assert f"worktree add --detach /tmp/land-999 {'m' * 40}" in msg
    assert "land.sh --pr 999 --since pre" in msg


def test_a_stale_checkout_names_the_primary_once_it_carries_master():
    ln, _ = make_landing(Fakes(stale_code=["a.py"], merge_applied_rc=0))
    ln.merge_sha = "m" * 40
    with pytest.raises(Outcome) as raised:
        classify.refuse_stale_code(ln)
    assert f"re-run it from {PRIMARY}" in raised.value.detail
    assert f"--since {'m' * 40}^1" in raised.value.detail


def test_a_comparison_git_could_not_make_refuses():
    ln, _ = make_landing(Fakes(stale_code=None))
    ln.merge_sha = "m" * 40
    with pytest.raises(Outcome, match="git could not compare"):
        classify.refuse_stale_code(ln)


def test_a_stale_checkout_ends_the_landing_refused_before_it_classifies(land_run):
    fakes = Fakes(stale_code=["scripts/lib/render_guard.py"])
    rc, out, err, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "scripts/lib/render_guard.py" in err
    assert not any(c[0] == "deploy_tags" for c in calls)
