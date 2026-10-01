"""`lib.proc_testing` — the shared test runner and fake-binary writer.

This module's subject IS the three decisions `proc_testing` centralises (#3056): the launch
flags, the `PATH` prefix and the exec bit. It therefore writes executables and `PATH` strings
by hand, and is exempt from the guard in
`scripts/tests/test_tests_share_the_subprocess_helpers.py` for that reason.

Run: uv run pytest scripts/lib/tests/test_proc_testing.py
"""

import os
import subprocess

import pytest

from lib.proc_testing import (
    DEFAULT_TIMEOUT,
    fake_bin,
    path_with,
    run,
    run_out,
    write_exec,
)


def test_a_written_script_is_executable_and_runs(tmp_path):
    script = write_exec(tmp_path / "greet", "#!/bin/sh\necho hi\n")
    assert os.access(script, os.X_OK)
    assert run_out([str(script)]) == "hi"


def test_a_body_without_a_shebang_still_execs(tmp_path):
    """The bug this prevents: a stub written without `#!` fails as `Exec format error`."""
    script = write_exec(tmp_path / "bare", "echo hi\n")
    assert script.read_text().startswith("#!")
    assert run_out([str(script)]) == "hi"


def test_a_parent_directory_is_created(tmp_path):
    script = write_exec(tmp_path / "deep" / "nest" / "s", "echo ok\n")
    assert run_out([str(script)]) == "ok"


def test_fake_bin_writes_one_stub_per_keyword(tmp_path):
    stubs = fake_bin(tmp_path / "bin", git="echo fake-git\n", curl="exit 7\n")
    assert sorted(p.name for p in stubs.iterdir()) == ["curl", "git"]
    assert run([str(stubs / "curl")]).returncode == 7


def test_fake_bin_reuses_a_directory_so_two_calls_build_one_up(tmp_path):
    bin_dir = tmp_path / "bin"
    fake_bin(bin_dir, git="echo a\n")
    fake_bin(bin_dir, curl="echo b\n")
    assert sorted(p.name for p in bin_dir.iterdir()) == ["curl", "git"]


def test_stub_bin_shadows_the_real_binary(tmp_path):
    """The property the whole module exists for: the stub is found before `/usr/bin`."""
    stubs = fake_bin(tmp_path / "bin", env="echo shadowed\n")
    assert run_out(["env"], stub_bin=stubs) == "shadowed"


def test_the_real_path_survives_behind_the_stub_directory(tmp_path):
    """A prefix, not a replacement — the child still finds `sh` and everything else."""
    stubs = fake_bin(tmp_path / "bin", nothing_real="exit 0\n")
    found = run_out(["sh", "-c", "command -v true"], stub_bin=stubs)
    assert found != ""


def test_path_with_prefixes_in_the_order_given():
    built = path_with("/a", "/b", env={"PATH": "/usr/bin"})
    assert built == f"/a{os.pathsep}/b{os.pathsep}/usr/bin"


def test_path_with_on_an_environment_that_has_no_path():
    """A deliberately minimal environment gets the prefix alone, not a trailing separator."""
    assert path_with("/a", env={}) == "/a"


def test_output_is_text_and_captured():
    done = run(["sh", "-c", "printf out; printf err >&2"])
    assert (done.stdout, done.stderr) == ("out", "err")


def test_a_nonzero_exit_is_returned_rather_than_raised():
    """A test asserting on an exit code has to be able to read it, so `check` defaults off."""
    assert run(["sh", "-c", "exit 3"]).returncode == 3


def test_check_raises_when_asked():
    with pytest.raises(subprocess.CalledProcessError):
        run(["sh", "-c", "exit 3"], check=True)


def test_input_reaches_the_child():
    assert run_out(["cat"], input="fed\n") == "fed"


def test_a_wedged_child_times_out_rather_than_parking_the_run():
    with pytest.raises(subprocess.TimeoutExpired):
        run(["sleep", "5"], timeout=0.2)


def test_the_default_timeout_is_bounded():
    """A regression guard: a `None` default would make every caller hang-prone again."""
    assert isinstance(DEFAULT_TIMEOUT, int | float) and DEFAULT_TIMEOUT > 0


def test_a_replacement_environment_is_used_rather_than_merged():
    done = run(
        ["sh", "-c", "echo ${ONLY-unset}"], env={"ONLY": "yes", "PATH": "/usr/bin:/bin"}
    )
    assert done.stdout.strip() == "yes"


def test_cwd_decides_where_the_child_runs(tmp_path):
    assert run_out(["pwd"], cwd=tmp_path) == str(tmp_path.resolve())


def test_shell_runs_a_string(tmp_path):
    assert run_out("echo shelled", shell=True) == "shelled"
