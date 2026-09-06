"""The leak guard's own red-proof.

`ansible/tests/leakguard.py` fails any test that reaches the network, the cluster or the host.
A guard is only ever observed passing, so per the repo's rule every rule here is an
accept/reject pair: one input it must let through and one it must fail. A guard that fires on
everything and one that fires on nothing are indistinguishable from the passing side alone.

The two subprocess tests drive a real pytest run because that is the only thing that exercises
the hook wiring — `pytest_runtest_setup` putting the stub dir on PATH and
`pytest_runtest_teardown` failing the test that used it. Calling the helpers directly would
prove the helpers work and say nothing about whether the plugin is loaded.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import leakguard
from _helpers import REPO


# A bare ini so the repo's own addopts (`-n auto`, `filterwarnings = error`) do not reach the
# child run. The child needs only the plugin under test.
# `markers` registers the one the `ui` exemption keys on, so the child does not emit an
# unknown-mark warning that reads like the marker not having applied.
_CHILD_INI = "[pytest]\nmarkers =\n    ui: stands in for the live-browser suite\n"

_LEAKS = """
import subprocess


def test_it_shells_out():
    subprocess.run(["curl", "-sS", "https://example.invalid"], check=False)
"""

_CLEAN = """
def test_it_touches_nothing():
    assert 1 + 1 == 2
"""

# One child module carrying both halves of the `ui` exemption, so a single run shows the
# marker deciding rather than the guard having been switched off. `curl` here resolves
# `example.invalid`, which is NXDOMAIN by RFC 6761 — nothing leaves the host either way.
_UI_MARKED_AND_PLAIN = """
import subprocess

import pytest


@pytest.mark.ui
def test_a_ui_test_shells_out():
    proc = subprocess.run(["curl", "-sS", "https://example.invalid"], check=False,
                          capture_output=True, text=True)
    assert "is stubbed during tests" not in proc.stderr, proc.stderr


def test_a_plain_test_shells_out():
    subprocess.run(["curl", "-sS", "https://example.invalid"], check=False)
"""

# A skipped test never reaches `pytest_runtest_setup`, but its teardown still runs. Ordered
# so that the skip follows the leak: the guard must blame the test that shelled out and leave
# the skip alone.
_LEAKS_THEN_SKIPS = """
import subprocess

import pytest


def test_it_shells_out():
    subprocess.run(["curl", "-sS", "https://example.invalid"], check=False)


@pytest.mark.skip(reason="stands in for the live-API tests, which skip where there is no cluster")
def test_it_is_skipped():
    raise AssertionError("this body must never run")
"""


# The socket probe's half of the same exemption. 192.0.2.1 is TEST-NET-1 (RFC 5737): reserved,
# never routed, so the exempt half reaches the real syscall without anything leaving the host.
# The exempt test asserts only that the failure is NOT the guard's — which errno a real socket
# returns varies by host, and a timeout raises rather than returning one.
_UI_MARKED_AND_PLAIN_CONNECT = """
import socket

import pytest


def _connect():
    with socket.socket() as sock:
        sock.settimeout(0.25)
        sock.connect(("192.0.2.1", 9))


@pytest.mark.ui
def test_a_ui_test_connects_in_process():
    try:
        _connect()
    except RuntimeError as exc:
        raise AssertionError(f"the guard fired on an exempt test: {exc}") from exc
    except OSError:
        pass


def test_a_plain_test_connects_in_process():
    try:
        _connect()
    except OSError:
        pass
"""


def _run_child(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text(_CHILD_INI)
    test_file = tmp_path / "test_child.py"
    test_file.write_text(body)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "leakguard",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            str(test_file),
            "-q",
        ],
        cwd=tmp_path,
        # `ansible/tests` is on pythonpath for the parent through pyproject.toml; the child
        # runs under its own bare ini, so it needs the path spelled out.
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO / "ansible" / "tests")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_test_that_shells_out_to_a_shimmed_binary_is_failed(tmp_path: Path) -> None:
    """Reject case: the guard turns an unstubbed `curl` into a failure naming the test."""
    proc = _run_child(tmp_path, _LEAKS)
    assert proc.returncode != 0, (
        "the guard let a test shell out to curl without failing it:\n" + proc.stdout
    )
    assert "LEAKGUARD" in proc.stdout, proc.stdout
    assert "test_it_shells_out" in proc.stdout, proc.stdout


def test_a_test_that_touches_nothing_passes(tmp_path: Path) -> None:
    """Accept case: the guard is not simply failing everything."""
    proc = _run_child(tmp_path, _CLEAN)
    assert proc.returncode == 0, (
        "the guard failed a test that touches nothing:\n" + proc.stdout + proc.stderr
    )


def test_a_skipped_test_does_not_inherit_the_previous_test_s_calls(
    tmp_path: Path,
) -> None:
    """The leak is blamed on the test that caused it, even when the next one skips.

    Teardown runs for a test whose setup never did, so a guard holding one mark rather than
    one per nodeid hands the earlier test's calls to the later one. Reject case for that:
    exactly one failure, naming the test that shelled out.
    """
    proc = _run_child(tmp_path, _LEAKS_THEN_SKIPS)
    assert proc.returncode != 0, proc.stdout
    # The shim's own stderr also says LEAKGUARD, so match the blame line specifically.
    blamed = [
        line
        for line in proc.stdout.splitlines()
        if "reached outside the test process" in line
    ]
    assert len(blamed) == 1, (
        "the leak was blamed on more than one test:\n" + proc.stdout
    )
    assert "test_it_shells_out" in blamed[0], proc.stdout
    assert "test_it_is_skipped" not in blamed[0], (
        "the skipped test was blamed for the leaking test's calls:\n" + proc.stdout
    )


def test_a_ui_marked_test_is_clean_when_it_shells_out(tmp_path: Path) -> None:
    """Accept case for the `ui` exemption: a live-browser test reaches the real binaries.

    Its fixtures decrypt SOPS before anything renders, so a shimmed `sops` errors the tier in
    setup instead of catching a leak.
    """
    proc = _run_child(tmp_path, _UI_MARKED_AND_PLAIN)
    # The guard fails a leaking test from teardown, which pytest reports as an error rather
    # than a failure — so the passing count is what says the `ui` test came through.
    assert "2 passed" in proc.stdout, (
        "the guard failed a `ui`-marked test for shelling out:\n" + proc.stdout
    )
    assert "test_a_ui_test_shells_out" not in proc.stdout, (
        "the `ui`-marked test was named in the summary, so it did not pass clean:\n"
        + proc.stdout
    )


def test_an_unmarked_test_in_the_same_run_is_flagged(tmp_path: Path) -> None:
    """Reject case for the same pair: the exemption is the marker, not the whole run."""
    proc = _run_child(tmp_path, _UI_MARKED_AND_PLAIN)
    assert proc.returncode != 0, proc.stdout
    blamed = [
        line
        for line in proc.stdout.splitlines()
        if "reached outside the test process" in line
    ]
    assert len(blamed) == 1, (
        "the unmarked test alone should have been blamed:\n" + proc.stdout
    )
    assert "test_a_plain_test_shells_out" in blamed[0], proc.stdout


def test_a_ui_marked_test_may_connect_in_process(tmp_path: Path) -> None:
    """Accept case for the socket half of the `ui` exemption.

    The marker lifted the PATH shims alone until 2026-09-06, so a `ui` test that curls a LAN
    route from pytest itself — rather than from the browser subprocess — hit
    `LEAKGUARD: blocked a network connect`, a message naming the guard but not the exemption.
    """
    proc = _run_child(tmp_path, _UI_MARKED_AND_PLAIN_CONNECT)
    assert "1 passed" in proc.stdout, (
        "the guard blocked an in-process connect from a `ui`-marked test:\n"
        + proc.stdout
    )
    assert "test_a_ui_test_connects_in_process" not in proc.stdout, (
        "the `ui`-marked test was named in the summary, so it did not pass clean:\n"
        + proc.stdout
    )


def test_an_unmarked_test_connecting_in_process_is_flagged(tmp_path: Path) -> None:
    """Reject case for the same pair: the socket probe is lifted by the marker, not by the run.

    Guards against the fix over-reaching — a flag left set after the exempt test would exempt
    every later test in the worker, which reads exactly like a pass.
    """
    proc = _run_child(tmp_path, _UI_MARKED_AND_PLAIN_CONNECT)
    assert proc.returncode != 0, proc.stdout
    blamed = [
        line
        for line in proc.stdout.splitlines()
        if "reached outside the test process" in line
    ]
    assert len(blamed) == 1, (
        "the unmarked test alone should have been blamed:\n" + proc.stdout
    )
    assert "test_a_plain_test_connects_in_process" in blamed[0], proc.stdout


def test_the_guard_is_on_before_any_test_owns_the_process() -> None:
    """A missing `exempt` key must fail closed — collection and session fixtures run there."""
    saved = leakguard._state.pop("exempt", None)
    try:
        assert leakguard._is_exempt() is False
        leakguard._state["exempt"] = True
        assert leakguard._is_exempt() is True
        leakguard._state["exempt"] = False
        assert leakguard._is_exempt() is False
    finally:
        leakguard._state.pop("exempt", None)
        if saved is not None:
            leakguard._state["exempt"] = saved


def test_every_deselected_marker_in_addopts_is_one_the_guard_exempts(
    pytestconfig: pytest.Config,
) -> None:
    """A tier `addopts` deselects is a tier only a person typing `-m` by hand ever runs.

    That is how leakguard made the whole `-m ui` suite error in fixture setup for four days
    with every guard green (issue #1300): CI never reached it. Any marker deselected by
    default is in the same position, so each one must also be exempt from the guard — a new
    `-m 'not ui and not slow'` fails here rather than four days later.

    Set EQUALITY, not containment: it doubles as the non-vacuity assertion, so a parse that
    finds no markers fails instead of passing over an empty set.
    """
    # `getini` returns the ini value shell-split, so the expression after `-m` arrives as one
    # element with its quoting already resolved — and it stays the ini's own value even when
    # the run overrode `-m` on the command line.
    addopts = list(pytestconfig.getini("addopts"))
    deselected = set()
    for flag, expression in zip(addopts, addopts[1:], strict=False):
        if flag != "-m":
            continue
        for clause in expression.split(" and "):
            clause = clause.strip()
            if clause.startswith("not "):
                deselected.add(clause.removeprefix("not ").strip())
    assert deselected == {leakguard._LIVE_MARKER}, (
        f"addopts deselects {sorted(deselected)} by default, but the leak guard exempts only "
        f"`{leakguard._LIVE_MARKER}`. A deselected tier never runs in CI, so a guard that "
        "breaks it stays green — exempt the marker in ansible/tests/leakguard.py, or say here "
        "why this one is safe to leave guarded."
    )


@pytest.mark.parametrize("module", ["test_ui_smoke.py", "test_ui_smoke_grafana.py"])
def test_the_live_marker_is_one_the_ui_suite_actually_carries(module: str) -> None:
    """Non-vacuity: a renamed marker would exempt nothing and read exactly like a pass.

    Both live-browser modules, not just one: the Grafana tier was split out of the other in
    2026-09-06, and it is the half whose fixtures decrypt SOPS for a second credential.
    """
    suite = REPO / "scripts" / "diagnostics" / "tests" / module
    assert f"pytestmark = pytest.mark.{leakguard._LIVE_MARKER}" in suite.read_text(), (
        f"{suite} no longer carries the `{leakguard._LIVE_MARKER}` marker the leak guard "
        "exempts, so its whole live-browser tier is back to erroring on a stubbed `sops`"
    )


def test_a_non_loopback_address_is_not_local() -> None:
    assert leakguard._is_loopback(("1.1.1.1", 443)) is False
    assert leakguard._is_loopback(("10.0.0.240", 443)) is False


def test_a_loopback_or_unix_address_is_local() -> None:
    assert leakguard._is_loopback(("127.0.0.1", 8080)) is True
    assert leakguard._is_loopback(("::1", 8080)) is True
    # AF_UNIX passes a path, not a peer.
    assert leakguard._is_loopback("/run/some.sock") is True


def test_every_allowlisted_nodeid_names_a_test_that_exists() -> None:
    """Non-vacuity: an allowlist that quietly empties would make the guard pass everything.

    A renamed or deleted live-API test must fail HERE, naming the entry, rather than leaving a
    stale nodeid that matches nothing. Nine guards in this repo have broken by globbing for
    their own subject and finding none of it; the fix is always to assert against something
    concrete.
    """
    assert len(leakguard._LIVE_API_TESTS) >= 5, (
        "the live-API allowlist has shrunk below the five tests measured on 2026-09-04 — if "
        "one was retired, say so here"
    )
    for nodeid in sorted(leakguard._LIVE_API_TESTS):
        relative, _, test_name = nodeid.partition("::")
        path = REPO / relative
        assert path.is_file(), f"{nodeid} names a file that does not exist"
        assert f"def {test_name}(" in path.read_text(), (
            f"{nodeid} names a test that {relative} no longer defines"
        )


@pytest.mark.parametrize(
    "binary", ["ssh", "curl", "kubectl", "docker", "sops", "gh", "logger"]
)
def test_the_shim_set_still_covers_the_core_egress_binaries(binary: str) -> None:
    """Non-vacuity for the other half: an emptied tuple would shim nothing and pass."""
    assert binary in leakguard.SHIMMED_BINARIES
