"""Launch a subprocess from a test, and build the fake binaries it finds on `PATH`.

WHY A SHARED MODULE. A test that drives a shell script or an entry point does the same three
things every time, and the 2026-09-28 tests audit found every caller deciding all three for
itself (#3056).

1. **The launch flags.** `text=True` and `capture_output=True` are what a test wants in every
   case — it asserts on `stdout`, so bytes and an uncaptured stream are both wrong — and a
   missing `timeout=` turns a wedged child into a CI run that never ends. Sixty-four local
   `_run`/`run_script` wrappers re-derived that trio, and they disagreed on the timeout.
2. **The environment.** Most callers built `{**os.environ, "PATH": f"{stub}{os.pathsep}..."}`
   by hand. A caller that runs `git` wants `lib.git_testing.scrubbed_env()` underneath instead,
   and spelling the merge out at each site is how one site forgets.
3. **The fake binary.** A stub `git` or `curl` is a file that needs a `#!` line, an exec bit and
   its directory in front of the real `PATH`. Eleven writers re-decided each part; a writer that
   forgets the exec bit fails as `Permission denied` from inside the script under test, which
   reads as a bug in the script.

This module is the positive form of all three: `fake_bin` writes the stubs, and `run` takes the
directory it returns as `stub_bin=` so the `PATH` prefix is decided once.

Import it from any test, at any depth — `pyproject.toml` puts `scripts/` on `pythonpath`::

    from lib.proc_testing import fake_bin, run

    stubs = fake_bin(tmp_path / "bin", git='#!/bin/sh\\necho stub\\n')
    done = run(["bash", str(script)], stub_bin=stubs, cwd=tmp_path)

`lib.git_testing` is the sibling for a scratch git repository, and the two compose: pass
`env=scrubbed_env()` to `run` when the child shells out to git.

A test whose SUBJECT is one of the three decisions does not use this. `scripts/lib/tests/
test_proc_testing.py` covers this module's own behaviour, and a test that pins the minimal
environment its subject runs under — a hook fixture holding `PATH` to `/usr/bin:/bin` — keeps
its raw form. Both cases are named in the guard's exemption set with that reason.

Run: uv run pytest scripts/lib/tests/test_proc_testing.py
"""

import os
import subprocess
from pathlib import Path

__all__ = [
    "DEFAULT_TIMEOUT",
    "SHEBANG",
    "fake_bin",
    "path_with",
    "run",
    "write_exec",
]

# Every launch this module makes is bounded. 60s is two orders of magnitude above what a test
# launch here actually takes (`pytest scripts/deploy_tools/tests/test_deploy_at_sha.py
# --durations=5` on daniel-server, 2026-10-01: slowest case 0.39s), so it leaves room for a
# loaded CI runner without any caller needing to raise it. It is also far below pytest's own
# patience, which is the point — a child that wedges fails its own test rather than parking the
# run. A caller whose subject is genuinely slow passes its own `timeout=`; the `ansible-lint`
# hook test passes 120.
DEFAULT_TIMEOUT = 60

# The interpreter line `write_exec` supplies when the text does not carry one. `sh` rather than
# `bash`, so a stub runs the same under a container image that ships neither as `/bin/bash`.
SHEBANG = "#!/bin/sh\n"


def write_exec(path: Path, text: str) -> Path:
    """Write `text` to `path` as an executable script, and return `path`.

    Args:
        path: Where the script goes. Its parent is created if it does not exist.
        text: The script body. A `#!` first line is kept as written; text without one gets
            `SHEBANG` prepended, so a caller passing a bare `echo` line still gets a file the
            kernel can exec.

    The exec bit is set for everyone (`0o755`) rather than for the owner alone: a test that
    runs its stub through `sudo` or as another user under `become` needs the group and other
    bits. Two callers do — `ansible/tests/longhorn/test_volume_revert_input_guard.py` and
    `test_volume_snapshot_register.py` both hand a `fake_become` stub to a real playbook run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.startswith("#!") else SHEBANG + text)
    path.chmod(0o755)
    return path


def fake_bin(directory: Path, **scripts: str) -> Path:
    """Write one executable stub per keyword into `directory`, and return `directory`.

    A keyword name is the binary's name, so a stub `git` and `curl` pair is one call::

        fake_bin(tmp_path / "bin", git="echo 0\\n", curl="exit 7\\n")

    Pass the returned directory to `run(..., stub_bin=...)`; that is the only sanctioned way to
    put it in front of the real `PATH`.

    Args:
        directory: Where the stubs go. Created if it does not exist, and reused if it does, so
            two calls can build up one directory.
        **scripts: name -> script body, each written through `write_exec`.

    Returns:
        `directory`, so the call can be inlined into a `stub_bin=` argument.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in scripts.items():
        write_exec(directory / name, text)
    return directory


def path_with(*directories: Path | str, env: dict[str, str] | None = None) -> str:
    """The `PATH` value that finds `directories` FIRST, then everything the real one finds.

    Args:
        *directories: Prepended in the order given, so the first argument wins a name clash.
        env: The environment whose `PATH` is being extended. Defaults to this process's.
            A caller that already built an environment passes it, rather than letting the
            prefix land on a `PATH` its child never sees.

    Returns:
        A `PATH` string. An environment with no `PATH` at all yields the prefix alone, which is
        what a deliberately minimal environment wants.
    """
    base = (os.environ if env is None else env).get("PATH", "")
    prefix = os.pathsep.join(str(d) for d in directories)
    return f"{prefix}{os.pathsep}{base}" if base else prefix


def run(
    argv: list[str] | str,
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    stub_bin: Path | str | None = None,
    input: str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    check: bool = False,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run `argv` captured as text, under a bounded timeout, and return the completed process.

    Args:
        argv: The command. A list unless `shell=True`, where a string is the point.
        cwd: The directory to run in. Defaults to this process's.
        env: The child's environment. Defaults to this process's, extended rather than
            replaced, so a caller wanting the real `PATH` and one extra variable passes
            `{**os.environ, "X": "1"}` — or `scrubbed_env(X="1")` where the child runs git.
        stub_bin: A directory of fake binaries, put in FRONT of the environment's `PATH`.
            Pass what `fake_bin` returned. Handling the prefix here is the reason a caller
            never spells `os.pathsep` out.
        input: Text written to the child's stdin and then closed.
        timeout: Seconds before the child is killed and `TimeoutExpired` raised. `None` is
            allowed for the one case that needs it — a test whose subject IS the timeout — and
            is never the default.
        check: Raise `CalledProcessError` on a non-zero exit. Defaults to `False` because a
            test asserting on an exit code has to be able to read it.
        shell: Run through `/bin/sh`. For a subject that only exists as a shell line.

    Returns:
        A `CompletedProcess` with `str` `stdout` and `stderr`, never `None` — the capture is
        not optional, so an assertion on `stdout` cannot silently read a stream that went to
        the terminal instead.
    """
    environ = dict(os.environ if env is None else env)
    if stub_bin is not None:
        environ["PATH"] = path_with(stub_bin, env=environ)
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environ,
        input=input,
        timeout=timeout,
        check=check,
        shell=shell,
        capture_output=True,
        text=True,
    )
