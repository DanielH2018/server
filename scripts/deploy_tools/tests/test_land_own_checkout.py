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
from lib.git_testing import commit, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, path_with


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


def test_a_stale_checkout_names_the_file_and_the_fresh_rerun():
    ln, _ = make_landing(Fakes(stale_code=["scripts/lib/render_guard.py"]), since="pre")
    ln.merge_sha = "m" * 40
    with pytest.raises(Outcome) as raised:
        classify.refuse_stale_code(ln)
    msg = raised.value.detail
    assert raised.value.rc == 1 and "scripts/lib/render_guard.py" in msg
    assert "`land.sh --pr 999 --since pre --fresh`" in msg


def test_a_stale_checkout_without_since_reruns_from_the_merges_parent():
    ln, _ = make_landing(Fakes(stale_code=["a.py"]))
    ln.merge_sha = "m" * 40
    with pytest.raises(Outcome) as raised:
        classify.refuse_stale_code(ln)
    assert f"--since '{'m' * 40}^1' --fresh" in raised.value.detail


def test_a_comparison_git_could_not_make_refuses():
    ln, _ = make_landing(Fakes(stale_code=None))
    ln.merge_sha = "m" * 40
    with pytest.raises(Outcome, match="git could not compare"):
        classify.refuse_stale_code(ln)


def test_a_stale_checkout_ends_the_landing_refused_before_it_classifies(land_run):
    fakes = Fakes(stale_code=["scripts/lib/render_guard.py"])
    rc, out, _, calls, _ = land_run([], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "scripts/lib/render_guard.py" in out
    assert not any(c[0] == "deploy_tags" for c in calls)


def test_an_armed_landing_from_a_stale_checkout_refuses_before_it_merges(land_run):
    """#7830: the refusal at step 1 came after await_merge had already merged the PR."""
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Document the lander",
                "body": "",
            },
            "headRefOid": {"headRefOid": "c" * 40},
            "state,mergeable,headRefOid": [
                {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "c" * 40},
                {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": "c" * 40},
            ],
        },
        stale_code=["scripts/lib/render_guard.py"],
    )
    rc, out, _, calls, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "scripts/lib/render_guard.py" in out
    merges = [
        c for c in calls if c[0] == "gh" and any(a.endswith("/merge") for a in c[1])
    ]
    assert not merges, f"merged from a stale checkout: {merges}"


# Answers `log`/`diff` with the one path master changed, filtered by the pathspec it is given,
# so a path the comparison leaves out of its pathspec is never reported stale.
_GIT_REPORTING_A_MASTER_CHANGE = """#!{python}
import sys
args = sys.argv[1:]
changed = "scripts/lib/ansible_inventory.py"
if args and args[0] in ("log", "diff"):
    specs = args[args.index("--") + 1:] if "--" in args else []
    def covers(spec):
        spec = spec.rstrip("/")
        return spec in ("", ".", ":/") or changed == spec or changed.startswith(spec + "/")
    if not specs or any(covers(s) for s in specs):
        print(changed)
"""

_STALE_WITH_A_LAZY_MODULE_UNLOADED = """
import sys
sys.path[:0] = ["scripts", "scripts/deploy_tools"]
import land
from deploy_tools.land_lib import own_checkout
sys.modules.pop("lib.ansible_inventory", None)
sys.modules.pop("ansible_inventory", None)
print("\\n".join(own_checkout.stale_landing_code("origin/master") or []))
"""


def test_a_module_a_later_phase_imports_lazily_is_compared(tmp_path):
    """#7830: the census was `sys.modules`, so a module not yet imported was never compared."""
    bin_dir = fake_bin(
        tmp_path / "bin",
        git=_GIT_REPORTING_A_MASTER_CHANGE.format(python=sys.executable),
    )
    run = subprocess.run(
        [sys.executable, "-c", _STALE_WITH_A_LAZY_MODULE_UNLOADED],
        cwd=own_checkout.OWN_CHECKOUT,
        env={**scrubbed_env(), "PATH": path_with(bin_dir)},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert "scripts/lib/ansible_inventory.py" in run.stdout.split()


def test_imported_paths_follows_a_function_level_import_and_skips_the_venv(tmp_path):
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (tmp_path / "scripts" / "entry.py").write_text(
        "import json\n\ndef later():\n    from lib import lazy\n"
    )
    (lib / "lazy.py").write_text("from . import sibling\n")
    (lib / "sibling.py").write_text("")
    venv = tmp_path / ".venv" / "pkg.py"
    venv.parent.mkdir()
    venv.write_text("from lib import unused\n")
    (lib / "unused.py").write_text("")
    got = own_checkout.imported_paths(
        tmp_path, [tmp_path / "scripts"], ["scripts/entry.py", ".venv/pkg.py"]
    )
    assert got == [
        ".venv/pkg.py",
        "scripts/entry.py",
        "scripts/lib/lazy.py",
        "scripts/lib/sibling.py",
    ]


def test_a_comparison_git_could_not_make_refuses_before_it_merges(land_run):
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Document the lander",
                "body": "",
            },
            "headRefOid": {"headRefOid": "c" * 40},
            "state,mergeable,headRefOid": [
                {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "c" * 40},
                {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": "c" * 40},
            ],
        },
        stale_code=None,
    )
    rc, out, _, calls, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    assert rc == 1 and "VERDICT: refused" in out
    assert "git could not compare" in out
    merges = [
        c for c in calls if c[0] == "gh" and any(a.endswith("/merge") for a in c[1])
    ]
    assert not merges, f"merged when git could not compare: {merges}"


def _armed_open_pr() -> dict:
    return {
        "state,title,body,reviewDecision": {
            "state": "OPEN",
            "title": "Document the lander",
            "body": "",
        },
        "headRefOid": {"headRefOid": "c" * 40},
        "state,mergeable,headRefOid": [
            {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "c" * 40},
            {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": "c" * 40},
        ],
    }


# Before the merge (#7916's refusal) and after it, from a branch master has moved past.
_STALE_LANDINGS = {
    "before-merge": (["--arm-merge", "--await-merge"], _armed_open_pr()),
    "after-merge": ([], None),
}


def _stale_landing_output(land_run, case: str) -> str:
    argv, views = _STALE_LANDINGS[case]
    fakes = Fakes(stale_code=["scripts/deploy_tools/land_lib/classify.py"])
    if views is not None:
        fakes.gh_views = views
    _, out, err, _, _ = land_run(argv, fakes)
    return out + err


@pytest.mark.parametrize("case", sorted(_STALE_LANDINGS))
def test_a_stale_landing_prints_no_remedy_that_redirects_git_to_the_primary(
    land_run, case
):
    """#7933: a worktree-isolated session's guard denies `git -C <primary>`."""
    printed = _stale_landing_output(land_run, case)
    assert "git -C" not in printed
    assert f"-C {PRIMARY}" not in printed


@pytest.mark.parametrize("case", sorted(_STALE_LANDINGS))
def test_a_stale_landing_needs_no_hand_made_checkout(land_run, case):
    """#7933: a checkout refreshed by hand went stale again within minutes (#7902)."""
    printed = _stale_landing_output(land_run, case)
    assert "worktree add" not in printed
