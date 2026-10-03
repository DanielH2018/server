"""Shared fixtures for the SessionStart hook tests.

`test_the_hook_can_import_prune_worktrees_when_run_as_a_subprocess` runs session-health.py as
a real subprocess (deliberately -- see that test's own docstring), which means `main()` runs
for real and reaches whatever it reaches: `gh pr list` once per stale worktree candidate (an
authenticated GitHub API call), `sops -d` to decrypt `ansible/vars/secrets.yml` for the
`domain` key, `curl` against the live Prometheus endpoint, and `journalctl` for the
release-staleness cron's last verdict. A main() test that hand-rolled its own monkeypatches
instead of going through `_run_main` would let `stale_worktree_lines` run for real too and
make `gh` calls. None of that is what either test is checking.

Same mechanism as `ansible/tests/_helpers.py`'s `stub_logger_on_path`, which
`scripts/deploy_tools/tests/conftest.py`'s `_no_syslog` calls. Both write their stubs and their
`PATH` through `lib.proc_testing`; what stays per-suite is WHICH binaries each fences and what
their bodies record.
"""

import sys
from pathlib import Path

import pytest

from lib.proc_testing import fake_bin, path_with

# One line per invocation, so a test can assert the stub intercepted a call rather than the
# real binary running underneath it. A stub that silently drops off PATH fails OPEN -- the
# run stays green and the real binary (real gh auth, a real SOPS decrypt, a real curl to
# Prometheus) takes every call again.
_STUB_TEMPLATE = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "$FENCE_CALLS"
"""

_FENCED_BINARIES = ("gh", "sops", "curl", "journalctl")


@pytest.fixture(autouse=True)
def _fence_external_binaries(tmp_path_factory, monkeypatch):
    """Put no-op recording stand-ins for `_FENCED_BINARIES` first on PATH.

    Returns the file every stub appends its argv to, one line per call as
    `<binary> <args...>`.
    """
    stub_dir = fake_bin(
        tmp_path_factory.mktemp("bin-stub"),
        **{name: _STUB_TEMPLATE.format(name=name) for name in _FENCED_BINARIES},
    )
    calls = stub_dir / "calls"
    calls.touch()
    monkeypatch.setenv("FENCE_CALLS", str(calls))
    monkeypatch.setenv("PATH", path_with(stub_dir))
    return calls


@pytest.fixture
def fenced_calls(_fence_external_binaries):
    """The file the stubbed `_FENCED_BINARIES` append to, one line per call."""
    return _fence_external_binaries


HOOKS = Path(__file__).resolve().parent.parent
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
