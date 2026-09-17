"""Shared fixtures for the SessionStart hook tests, plus a `claude_guard` stand-in for CI.

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

The module-level block below is unrelated to the fixtures above: it is an in-process
stand-in for `claude_guard` when the real package is not deployed. Every CI runner hits
this -- `_readonly_tables.py` now imports `claude_guard.tables` through `_claude_guard.py`,
which raises `ImportError` when `~/.local/share/claude-guard` is absent (see
`_claude_guard.py`'s own docstring for why that is the correct behaviour for the deployed
hook). CI never runs chezmoi, so without this stand-in every test that merely imports
`_readonly_tables.py` or `auto-approve-readonly.py` -- not just the ssh-specific ones --
would fail at collection, including the 155-vector table this suite calls a security
boundary.

This is NOT the fallback `_claude_guard.py` refuses to add. It never touches
site-packages, it is built fresh in THIS pytest process and never written to disk, and it
never reaches the hook's own runtime: `auto-approve-readonly.sh` /
`auto-approve-remote-ssh.sh` each exec a fresh `uv run --no-sync python <script>.py`
subprocess per command, which starts with its own `sys.modules` and never sees anything
this conftest did. A host missing the real deploy still gets `_claude_guard.py`'s hard
`ImportError` the moment Claude Code actually runs the hook; only this test session is
padded, and only when the real package is unreachable from it.

`test_claude_guard_import.py::test_deployed_import_reaches_both_trusted_hosts` is the one
test this stand-in cannot help -- it runs the real subprocess invocation, so it skips
(rather than passing on faked data) wherever the real package is absent.

DECIDED: the two `STAND_IN_*` values below are a second copy, by construction -- this
stand-in exists only because CI cannot reach the real one. They are module constants rather
than literals inside the `except` so that a machine WITH the real package can diff them:
`test_claude_guard_import.py::test_the_ci_stand_in_matches_the_deployed_tables` does, and
`prek run` executes that test on every commit from a deployed host. CI itself cannot see the
drift; the deployed hosts' own runs are where it goes red.
"""

import os
import re
import sys
import types
from pathlib import Path

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


HOOKS = Path(__file__).resolve().parent.parent
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

STAND_IN_TRUSTED_SSH_HOSTS = frozenset({"daniel-server", "daniel-pi"})
# Byte-for-byte the deployed pattern: the diff test compares `.pattern`, not behaviour.
STAND_IN_SECRET_PATH_RE = re.compile(
    r"(\.env|\.ssh(/|\s|$)|id_rsa|id_ed25519|id_ecdsa|\.aws/credentials|\.aws/config|"
    r"\.gnupg(/|\s|$)|\.netrc|\.pypirc|\.npmrc|/secrets(/|\s|$)|\.git-credentials|"
    r"\.kube/config|\.docker/config\.json|\.config/gh/hosts\.yml|\.config/gcloud/|"
    r"\.config/rclone/rclone\.conf|terraform\.tfstate|\.bash_history|\.claude\.json|"
    r"/etc/shadow|/etc/gshadow|/proc/\S*environ|\.pem($|[^a-z])|\.key($|[^a-z])|"
    r"\.p12($|[^a-z])|\.pfx($|[^a-z]))",
    re.IGNORECASE,
)


@pytest.fixture
def claude_guard_stand_in():
    """The stand-in's two values, for the test that diffs them against the deployed tables."""
    return STAND_IN_TRUSTED_SSH_HOSTS, STAND_IN_SECRET_PATH_RE


try:
    import _claude_guard  # noqa: F401  (real bootstrap; populates sys.modules on success)
except ImportError:
    _fake_pkg = types.ModuleType("claude_guard")
    _fake_pkg.__path__ = []  # marks it as a package so `claude_guard.tables` resolves
    _fake_tables = types.ModuleType("claude_guard.tables")
    _fake_tables.TRUSTED_SSH_HOSTS = STAND_IN_TRUSTED_SSH_HOSTS
    _fake_tables.SECRET_PATH_RE = STAND_IN_SECRET_PATH_RE
    _fake_pkg.tables = _fake_tables
    sys.modules["claude_guard"] = _fake_pkg
    sys.modules["claude_guard.tables"] = _fake_tables
