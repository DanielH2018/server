"""Fail any test that reaches the network, the cluster or the host.

Registered as a plugin from `addopts` in pyproject.toml, so it covers every `testpaths`
entry rather than one directory at a time.

## Why a plugin rather than another conftest fixture

`_helpers.stub_logger_on_path` and `scripts/deploy_tools/tests/conftest.py` already do this
one binary and one directory at a time, and each was written after someone found the leak
somewhere else: issue #1052 found fixture verdicts on the Alert History board, and the
2026-09-04 sweep that produced #1057 found a test making five authenticated GitHub API calls,
a SOPS decrypt, two `docker ps` calls and a live Prometheus query on every run. Both are
after-the-fact detection of something that had been running green for months. This makes the
next one a red test instead.

## How it works

Two probes, both installed before the first test runs:

1. `socket.socket.connect`/`connect_ex` raise on any non-loopback peer.
2. A stub directory goes FIRST on PATH holding recording no-op shims for the binaries this
   repo shells out to. A test that wants to stub one for itself prepends its own directory
   later and still wins, so only calls nothing else intercepted reach these.

Either probe firing fails the test that caused it, by nodeid, at teardown.

## Two rules that keep it from breaking CI

**Only a binary that already exists gets a shim.** Several tests skip on
`shutil.which("kubectl") is None`, which is how they stay green on a GitHub runner with no
cluster. Shimming a binary the host lacks would make `which` find it, turn the skip into a
run, and fail CI. So the shim set is intersected with what is actually installed.

**An allowlisted test runs with the real PATH.** The five tests in `_LIVE_API_TESTS` exist to
run the role's own argv against a live API server; handing them a stub would leave them
passing while proving nothing, which is the failure mode this whole guard is about.

## What is deliberately NOT shimmed

`git` — too central to intercept safely, and the same sweep established the class is clean:
every write verb targets a `tmp_path` repo through `git -C`, `GIT_DIR`/`GIT_INDEX_FILE`
appear in zero child environments, and all twelve `clone`/`fetch` calls name a local
`tmp_path` origin.

`ansible-playbook` — six tests in `ansible/tests/longhorn/` deliberately run a real play
against `localhost`, and a stub returning a fixed exit code fails all six. Their remaining
side effect, the shared fact cache, is fenced at the source by `ANSIBLE_CACHE_PLUGIN=memory`
in the env those harnesses build.
"""

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

# Every binary here leaves the process: it talks to a daemon, a cluster, a remote host, or an
# API. A test needing one stubs it itself; this catches the ones that forgot.
SHIMMED_BINARIES = (
    "at",
    "crontab",
    "curl",
    "docker",
    "gh",
    "journalctl",
    "kubectl",
    "logger",
    "mosquitto_pub",
    "notify-send",
    "nsenter",
    "rsync",
    "scp",
    "sops",
    "ssh",
    "systemctl",
    "wget",
)

# Tests that run the role's own argv against a live API server on purpose. Each is documented
# in its module docstring and already guarded by `skipif(shutil.which("kubectl") is None)`
# plus a "no reachable cluster" skip, so they skip rather than fail where there is no cluster.
# They run with the real PATH: a stub would leave them green while checking nothing.
_LIVE_API_TESTS = frozenset(
    {
        "ansible/tests/deploy/test_cronjob_gate_decision.py::test_the_jsonpath_parses_against_the_live_api",
        "ansible/tests/longhorn/test_longhorn_api.py::test_the_resolve_returns_a_pod_ip_on_this_node",
        "ansible/tests/longhorn/test_volume_revert.py::test_the_listing_jsonpath_parses",
        "ansible/tests/longhorn/test_volume_snapshot.py::test_the_listing_fields_exist_on_a_real_snapshot",
        "ansible/tests/longhorn/test_volume_snapshot.py::test_the_listing_jsonpath_parses",
    }
)

# The shim records the call rather than only blocking it, so the failure message can name the
# argv. It exits non-zero and says so on stderr: a test that leaks then fails with a message
# naming this guard, instead of failing on a confusing empty read several asserts later.
_SHIM = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$LEAKGUARD_CALLS"
echo "LEAKGUARD: $(basename "$0") is stubbed during tests - see ansible/tests/leakguard.py" >&2
exit 127
"""

_state: dict[str, object] = {}


def _is_loopback(address: object) -> bool:
    """True for anything that does not leave this host.

    A non-tuple address is an AF_UNIX path or similar, which is local by construction.
    """
    if not isinstance(address, (tuple, list)) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return True
    return host.startswith("127.") or host in {"::1", "localhost", "", "0.0.0.0"}


def _install_shims(stub_dir: Path) -> list[str]:
    """Write a recording shim for each SHIMMED_BINARIES entry the host actually has.

    Returns the names shimmed. A binary the host lacks is skipped so that a
    `skipif(shutil.which(...) is None)` guard keeps skipping — see the module docstring.
    """
    installed = []
    for name in SHIMMED_BINARIES:
        if shutil.which(name) is None:
            continue
        shim = stub_dir / name
        shim.write_text(_SHIM)
        shim.chmod(0o755)
        installed.append(name)
    return installed


def pytest_configure(config: pytest.Config) -> None:
    stub_dir = Path(tempfile.mkdtemp(prefix="leakguard-"))
    calls = stub_dir / "calls"
    calls.touch()

    _state["stub_dir"] = str(stub_dir)
    _state["calls"] = calls
    _state["installed"] = _install_shims(stub_dir)
    _state["real_path"] = os.environ.get("PATH", "")

    os.environ["LEAKGUARD_CALLS"] = str(calls)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        if not _is_loopback(address):
            _record(f"socket connect {address!r}")
            raise RuntimeError(f"LEAKGUARD: blocked a network connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
        if not _is_loopback(address):
            _record(f"socket connect_ex {address!r}")
            raise RuntimeError(f"LEAKGUARD: blocked a network connect to {address!r}")
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]


def _record(line: str) -> None:
    calls = _state.get("calls")
    if isinstance(calls, Path):
        with calls.open("a") as handle:
            handle.write(line + "\n")


def _marks() -> dict[str, int]:
    """Where the call record stood when each running test started, keyed by nodeid."""
    marks = _state.setdefault("marks", {})
    assert isinstance(marks, dict)
    return marks


def _calls_seen() -> list[str]:
    calls = _state.get("calls")
    if not isinstance(calls, Path) or not calls.exists():
        return []
    return [line for line in calls.read_text().splitlines() if line.strip()]


def pytest_runtest_setup(item: pytest.Item) -> None:
    stub_dir = _state.get("stub_dir")
    real_path = _state.get("real_path")
    if not isinstance(stub_dir, str) or not isinstance(real_path, str):
        return
    # Remember where the record stood, so teardown can attribute only what THIS test added.
    # Keyed by nodeid rather than kept as a single value: teardown runs even when setup did
    # not — a `skipif` that fires, or a fixture that raises — and a single value would then
    # hand this test's calls to whichever test ran next. The five allowlisted tests all skip
    # on a runner with no cluster, so that path is exercised in CI and not here.
    _marks()[item.nodeid] = len(_calls_seen())
    if item.nodeid in _LIVE_API_TESTS:
        os.environ["PATH"] = real_path
    else:
        os.environ["PATH"] = f"{stub_dir}:{real_path}"


def pytest_runtest_teardown(item: pytest.Item) -> None:
    mark = _marks().pop(item.nodeid, None)
    if mark is None:
        return
    new = _calls_seen()[mark:]
    if not new:
        return
    joined = "\n  ".join(new)
    pytest.fail(
        f"LEAKGUARD: {item.nodeid} reached outside the test process:\n  {joined}\n\n"
        "Stub the call instead. `ansible/tests/_helpers.py::stub_logger_on_path` is the "
        "established shape: a recording stub first on PATH, plus one test asserting the stub "
        "RECORDED a call so it cannot fail open. If the test is meant to hit a live API, add "
        "its nodeid to `_LIVE_API_TESTS` in ansible/tests/leakguard.py and say why in the "
        "module docstring.",
        pytrace=False,
    )
