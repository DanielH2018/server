"""Shared fixtures for the SessionStart hook tests.

`test_the_hook_can_import_prune_worktrees_when_run_as_a_subprocess` runs session-health.py
as a real subprocess (deliberately -- see that test's own docstring), which means `main()`
runs for real and reaches whatever it reaches: `gh pr list` once per stale worktree
candidate (an authenticated GitHub API call), `sops -d` to decrypt `ansible/vars/secrets.yml`
for the `domain` key, `docker ps` x2, and `curl` against the live Prometheus endpoint.
`test_main_runs_targets_even_when_docker_down` used to hand-roll its own monkeypatches
instead of going through `_run_main`, so `stale_worktree_lines` ran for real too and made the
same 5 `gh` calls. Measured 2026-09-04. None of that is what either test is checking.

Same mechanism as `ansible/tests/_helpers.py`'s `stub_logger_on_path` and
`scripts/deploy_tools/tests/conftest.py`'s `_no_syslog`: not shared with them here, because
`_helpers.py` is importable from this directory only through `pyproject.toml`'s global
`pythonpath` setting, and generalizing it into a shared multi-binary stub is a separate
change from fencing this suite's leak.
"""

import os

import pytest

# One line per invocation, so a test can assert the stub intercepted a call rather than the
# real binary running underneath it. A stub that silently drops off PATH fails OPEN -- the
# run stays green and the real binary (real gh auth, a real SOPS decrypt, the real docker
# socket, a real curl to Prometheus) takes every call again.
_STUB_TEMPLATE = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "$FENCE_CALLS"
"""

_FENCED_BINARIES = ("gh", "sops", "docker", "curl")


@pytest.fixture(autouse=True)
def _fence_external_binaries(tmp_path_factory, monkeypatch):
    """Put no-op recording stand-ins for gh/sops/docker/curl first on PATH.

    Returns the file every stub appends its argv to, one line per call as
    `<binary> <args...>`.
    """
    stub_dir = tmp_path_factory.mktemp("bin-stub")
    calls = stub_dir / "calls"
    calls.touch()
    for name in _FENCED_BINARIES:
        stub = stub_dir / name
        stub.write_text(_STUB_TEMPLATE.format(name=name))
        stub.chmod(0o755)
    monkeypatch.setenv("FENCE_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return calls


@pytest.fixture
def fenced_calls(_fence_external_binaries):
    """The file the stubbed gh/sops/docker/curl append to, one line per call."""
    return _fence_external_binaries
