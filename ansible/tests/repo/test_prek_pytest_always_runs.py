"""Guard: the prek `pytest` hook must run unconditionally, with no `files` gate.

This replaces a guard that kept the hook's `files` regex in sync with pyproject's `testpaths`.
That regex is gone (see the comment on the hook in prek.toml), so the drift it policed can no
longer happen — but the reason it existed can come back the moment someone re-adds a `files`
gate to make PR runs marginally cheaper.

Why a gate here is always wrong: the hook is `pass_filenames = false`, so it runs the whole
suite whenever it fires. Gating it saves nothing when it runs and loses coverage when it
doesn't. And several tests in this repo discover their own inputs by walking the tree
(test_validate_shell_templates' pinned roster, test_healthchecks_pings' rglob), so a gate means
they cannot fail on the PR that breaks them — only on the push to master afterwards. That is
what put master red for six consecutive pushes on 2026-08-15.
"""

import tomllib
from _helpers import REPO as REPO_ROOT


def _pytest_hook() -> dict:
    data = tomllib.loads((REPO_ROOT / "prek.toml").read_text())
    for repo in data.get("repos", []):
        for hook in repo.get("hooks", []):
            if hook.get("id") == "pytest":
                return hook
    raise AssertionError("no `pytest` hook found in prek.toml")


def test_pytest_hook_always_runs() -> None:
    assert _pytest_hook().get("always_run") is True, (
        "the prek `pytest` hook must set always_run = true — without it, a PR that touches no "
        "matching path skips the whole suite, and CI scopes PR runs to changed files"
    )


def test_pytest_hook_has_no_files_gate() -> None:
    hook = _pytest_hook()
    assert "files" not in hook, (
        "the prek `pytest` hook must not have a `files` gate. always_run overrides it, so it "
        "would be dead config that reads as load-bearing; and if always_run is ever dropped it "
        "silently becomes a coverage gate again"
    )


def test_pytest_hook_takes_no_filenames() -> None:
    # The premise both tests above rest on: the hook runs the full suite rather than the files
    # prek hands it, which is why gating it can only ever subtract coverage.
    assert _pytest_hook().get("pass_filenames") is False
