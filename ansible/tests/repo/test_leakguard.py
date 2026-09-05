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
_CHILD_INI = "[pytest]\n"

_LEAKS = """
import subprocess


def test_it_shells_out():
    subprocess.run(["curl", "-sS", "https://example.invalid"], check=False)
"""

_CLEAN = """
def test_it_touches_nothing():
    assert 1 + 1 == 2
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
