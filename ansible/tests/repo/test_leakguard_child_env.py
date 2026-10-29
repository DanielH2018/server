"""leakguard's `Popen` wrapper: a child env built from scratch still gets the temp textfile dir.

`ansible/tests/leakguard.py` points `TEXTFILE_DIR` at a temp directory, but a test that hands
its child an explicit `env` used to drop it, and a check script the child ran wrote a fake
verdict into node_exporter's real directory (#6191, #6238). The same wrapper shims a
hand-built PATH without overriding a test's own stub or un-hiding a binary (#6418), and swaps
a real binary started by absolute path for its shim (#6463). These run in this process, under
the plugin `pyproject.toml` loads, because the wrapper is what they check.

Run: uv run pytest ansible/tests/repo/test_leakguard_child_env.py
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import leakguard
from _helpers import REPO
from lib.proc_testing import fake_bin
from test_leakguard import _run_child

_CHECK_LIB = REPO / "ansible/roles/setup/initial_setup/files/homelab-check-lib.sh"

# A child handed a PATH built from scratch, the shape of a test that pins `/usr/bin:/bin` for
# its child (#6418). The pytest PATH prefix never reaches it, so only the `Popen` wrapper can.
_LEAKS_THROUGH_A_HAND_BUILT_PATH = """
import subprocess


def test_it_logs_through_a_hand_built_path():
    subprocess.run(["logger", "leakguard-6418"], env={"PATH": "/usr/bin:/bin"}, check=False)
"""


# The real `logger` by absolute path, which no PATH reaches (#6463). None where the host has
# none, and the test that runs it skips there.
_REAL_LOGGER = shutil.which("logger", path="/usr/bin:/bin")

# Both ways a child names its program by absolute path: as `argv[0]`, and as `executable`
# beside a bare `argv[0]`. One child run each, because a teardown failure leaves the next test
# in the same run erroring in setup.
_ABSOLUTE_LAUNCHES = {
    "argv": f"[{_REAL_LOGGER!r}, 'leakguard-6463-argv']",
    "executable": f"['logger', 'leakguard-6463-executable'], executable={_REAL_LOGGER!r}",
}


def test_a_scratch_env_child_writes_its_textfile_under_the_temp_directory() -> None:
    textfile_dir = Path(os.environ["TEXTFILE_DIR"])
    scratch_env = {"PATH": "/usr/bin:/bin"}
    seen = subprocess.run(
        ["sh", "-c", 'echo "$TEXTFILE_DIR $LEAKGUARD_CALLS"'],
        env=scratch_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Asserted before the writer runs: a child that lacked it would write the real directory.
    assert seen.stdout.split() == [str(textfile_dir), os.environ["LEAKGUARD_CALLS"]]
    writer = f"source {_CHECK_LIB} && write_check_textfile leakguard-scratch 1 0 60"
    subprocess.run(["bash", "-c", writer], env=scratch_env, check=True, timeout=60)
    assert (textfile_dir / "leakguard-scratch.prom").is_file()


def test_a_scratch_env_child_keeps_its_own_textfile_dir(tmp_path: Path) -> None:
    """The wrapper fills only what the env lacks: the child's own TEXTFILE_DIR wins."""
    own = str(tmp_path / "own")
    seen = subprocess.run(
        ["sh", "-c", 'echo "$TEXTFILE_DIR $LEAKGUARD_CALLS"'],
        env={"PATH": "/usr/bin:/bin", "TEXTFILE_DIR": own},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seen.stdout.split() == [own, os.environ["LEAKGUARD_CALLS"]]


@pytest.mark.skipif(
    shutil.which("logger", path="/usr/bin:/bin") is None,
    reason="the host has no logger under /usr/bin:/bin, so a hand-built PATH reaches nothing",
)
def test_a_child_with_a_hand_built_path_is_failed_for_its_call(tmp_path: Path) -> None:
    """Reject case: `PATH=/usr/bin:/bin` no longer reaches the real `logger` unrecorded."""
    proc = _run_child(tmp_path, _LEAKS_THROUGH_A_HAND_BUILT_PATH)
    assert proc.returncode != 0, (
        "the guard let a hand-built PATH reach the real logger:\n" + proc.stdout
    )
    assert "reached outside the test process" in proc.stdout, proc.stdout
    assert "logger leakguard-6418" in proc.stdout, proc.stdout


@pytest.mark.skipif(
    _REAL_LOGGER is None,
    reason="the host has no logger under /usr/bin:/bin to run by path",
)
@pytest.mark.parametrize("form", sorted(_ABSOLUTE_LAUNCHES))
def test_a_child_started_by_absolute_path_is_failed_for_its_call(
    tmp_path: Path, form: str
) -> None:
    """Reject case: an absolute `argv[0]` or `executable` no longer reaches the real binary."""
    module = (
        "import subprocess\n\n\ndef test_it_logs_by_absolute_path():\n"
        f"    subprocess.run({_ABSOLUTE_LAUNCHES[form]}, check=False)\n"
    )
    proc = _run_child(tmp_path, module)
    assert proc.returncode != 0, (
        "the guard let an absolute path reach the real logger:\n" + proc.stdout
    )
    assert "reached outside the test process" in proc.stdout, proc.stdout
    assert f"logger leakguard-6463-{form}" in proc.stdout, proc.stdout


def test_a_tests_own_stub_started_by_absolute_path_still_runs(tmp_path: Path) -> None:
    """Accept case: only a program in a real PATH directory is swapped, not a test's stub."""
    fake_bin(tmp_path, logger="echo own-stub\n")
    seen = subprocess.run(
        [str(tmp_path / "logger"), "x"], capture_output=True, text=True, timeout=60
    )
    assert seen.stdout == "own-stub\n", seen.stderr


def test_a_hand_built_path_keeps_the_tests_own_stub_first(tmp_path: Path) -> None:
    """A stub the test put before `/usr/bin` still wins over leakguard's shim (#6418)."""
    fake_bin(tmp_path, logger="echo own-stub\n")
    seen = subprocess.run(
        ["logger", "x"],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seen.stdout == "own-stub\n", seen.stderr


def test_a_hand_built_path_that_hides_a_binary_still_hides_it(tmp_path: Path) -> None:
    """The wrapper adds no shim for a name the child's PATH cannot find."""
    seen = subprocess.run(
        ["/bin/sh", "-c", "command -v curl || echo hidden"],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seen.stdout == "hidden\n"


def test_a_symlink_to_the_real_binary_in_a_test_dir_is_the_tests_choice(
    tmp_path: Path,
) -> None:
    """Only a match in the real binary's own directory is shimmed, not a link to it."""
    real = shutil.which("sh", path="/usr/bin:/bin")
    assert real is not None
    (tmp_path / "sh").symlink_to(real)
    real_binaries = {"sh": leakguard._in_real_dir(real)}
    assert leakguard.names_reaching_real("/usr/bin:/bin", real_binaries) == ["sh"]
    assert (
        leakguard.names_reaching_real(f"{tmp_path}:/usr/bin:/bin", real_binaries) == []
    )


def test_only_a_real_stub_directory_is_dropped_from_the_recorded_path(
    tmp_path: Path,
) -> None:
    """A checkout under a `leakguard-*` directory keeps its `.venv/bin`; a stub does not."""
    checkout_bin = tmp_path / "leakguard-shim-fix" / ".venv" / "bin"
    checkout_bin.mkdir(parents=True)
    stub = tmp_path / "leakguard-abcd1234"
    subset = stub / "subset" / "curl+logger"
    subset.mkdir(parents=True)
    (stub / "calls").touch()
    path = os.pathsep.join([str(stub), str(subset), str(checkout_bin), "/usr/bin"])
    assert leakguard.without_stub_dirs(path) == os.pathsep.join(
        [str(checkout_bin), "/usr/bin"]
    )
