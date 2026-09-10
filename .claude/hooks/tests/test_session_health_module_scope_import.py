#!/usr/bin/env python3
"""No module-scope import in `session-health.py` can stop the banner (issue #1566).

The file defers `lib.git` and `prune_worktrees` into the functions that use them, and says so
at `parked_deployer_problems`: an import that fails there costs one `⚠` line, not the banner.
PR #1568 then added a module-scope `from lib.deployer_park import ...`, which made that claim
false — an ImportError there takes out the docker, scrape-target, live-session and
stale-worktree sections too, silently, because `session-health.sh` routes stderr to /dev/null
and exits 0.

Both halves: the hook must still run and REPORT the breakage when the import cannot resolve,
and it must still really import `lib.deployer_park` on a healthy checkout — a fallback that
quietly took over would leave the park detection dead behind a green test.

Run: uv run pytest .claude/hooks/tests/test_session_health_module_scope_import.py
"""

import importlib.util
import json
import os
import shutil
import subprocess

_HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOK = os.path.join(_HOOKS, "session-health.py")
# The same derivation the hook itself uses for REPO: its own directory, up three.
_REPO = os.path.dirname(os.path.dirname(_HOOKS))
_spec = importlib.util.spec_from_file_location("session_health", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_the_banner_survives_an_unresolvable_module_scope_import(tmp_path):
    """RED half: run a copy whose REPO has no `scripts/`, so the import cannot resolve.

    A copy rather than a patched `sys.path`, because the import runs at module scope and this
    pytest process has already put the real `scripts/` there. `REPO` is derived from the file's
    own location, so a copy two directories deep under tmp_path points at a tree with no
    `scripts/` — exactly the state an ImportError describes.
    """
    home = tmp_path / "a" / "b"
    home.mkdir(parents=True)
    copy = home / "session-health.py"
    shutil.copy(_HOOK, copy)

    result = subprocess.run(
        ["uv", "run", "--no-project", "python", str(copy)],
        input=json.dumps({"source": "startup"}),
        cwd=str(tmp_path),
        env=dict(os.environ, SESSION_HEALTH_VERBOSE="1"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert "parked-deployer detection is broken" in result.stdout, result.stdout


def test_only_the_park_module_missing_still_leaves_a_banner_naming_it(tmp_path):
    """RED half, isolated to the new branch: `lib.git` resolves and `lib.deployer_park` does not.

    Without this case the test above proves nothing about the module-scope wrapper — with no
    `scripts/` at all, the DEFERRED `lib.git` import fails first and the pre-existing `⚠` branch
    answers. Here `scripts/lib/` is present minus `deployer_park.py`, so the only import that
    can fail is the module-scope one, and the banner must name it.
    """
    home = tmp_path / "a" / "b"
    home.mkdir(parents=True)
    copy = home / "session-health.py"
    shutil.copy(_HOOK, copy)
    lib = tmp_path / "scripts" / "lib"
    shutil.copytree(os.path.join(_REPO, "scripts", "lib"), lib)
    os.remove(lib / "deployer_park.py")

    result = subprocess.run(
        ["uv", "run", "--no-project", "python", str(copy)],
        input=json.dumps({"source": "startup"}),
        cwd=str(tmp_path),
        env=dict(os.environ, SESSION_HEALTH_VERBOSE="1"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "parked-deployer detection is broken" in result.stdout, result.stdout
    assert "deployer_park" in result.stdout, result.stdout


def test_a_healthy_checkout_really_imports_the_shared_park_module():
    """CLEAN half: the wrapper must not become the path a healthy session takes."""
    from lib import deployer_park

    assert _mod.DEPLOYER_PARK_IMPORT_ERROR == ""
    # Identity against the shared module, not a second copy of its values: the point is that the
    # re-exported names still come from `lib.deployer_park` rather than from the fallback.
    assert _mod.BEHIND_PARK_SECONDS == deployer_park.BEHIND_PARK_SECONDS
    assert _mod.GITOPS_STATE_DIR == deployer_park.GITOPS_STATE_DIR
    assert _mod.read_behind_marker is deployer_park.read_behind_marker
