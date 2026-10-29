"""Fail any test that reaches the network, the cluster or the host.

Registered as a plugin from `addopts` in pyproject.toml, so it covers every `testpaths`
entry rather than one directory at a time.

## Why a plugin rather than another conftest fixture

`_helpers.stub_logger_on_path` and `scripts/deploy_tools/tests/conftest.py` cover one binary
and one directory at a time, and each detects a leak only after it has run green for months.
This plugin makes the next leak a red test instead.

## How it works

Two probes, both installed before the first test runs:

1. `socket.socket.connect`/`connect_ex` raise on any non-loopback peer.
2. A stub directory goes FIRST on PATH holding recording no-op shims for the binaries this
   repo shells out to. A test that wants to stub one for itself prepends its own directory
   later and still wins, so only calls nothing else intercepted reach these.

Either probe firing fails the test that caused it, by nodeid, at teardown.

An exemption lifts BOTH probes. `pytest_runtest_setup` asks `_runs_against_live_infra` once
and writes the answer to `_state["exempt"]` in the same branch that picks the PATH, so the
socket probe and the shims cannot disagree. A `ui` marker that lifted the shims alone would
fail an in-process request with `LEAKGUARD: blocked a network connect to (...)`, a message
naming the guard but not the exemption meant to cover it. `guarded_connect` reads the flag as `is True`,
which keeps the guard on wherever no test owns the process: collection, session-scoped
fixtures, and the window before the first test's setup. Teardown deliberately leaves the flag
alone; this hook runs before pytest finalizes fixtures, so clearing it here would fire the
guard inside a `ui` fixture's own teardown.

## Two rules that keep it from breaking CI

**Only a binary that already exists gets a shim.** Several tests skip on
`shutil.which("kubectl") is None`, which is how they stay green on a GitHub runner with no
cluster. Shimming a binary the host lacks would make `which` find it, turn the skip into a
run, and fail CI. So the shim set is intersected with what is actually installed.

**An allowlisted test runs with the real PATH.** The tests in `_LIVE_API_TESTS` exist to
run the role's own argv against a live API server; handing them a stub would leave them
passing while proving nothing, which is the failure mode this whole guard is about.

**A `ui`-marked test runs with the real PATH too.** `scripts/diagnostics/tests/test_ui_smoke.py`
drives a real Chromium against this homelab's LAN routes, and its fixtures read the SOPS
`domain` and `grafana_admin_password` before anything renders. Reaching live infrastructure is
the test there, so a stubbed `sops` errors the whole tier in fixture setup
with `could not decrypt domain`. The exemption
keys on the marker rather than on nodeids because that tier is parametrized:
`test_grafana_dashboard_renders_its_panels` gains a nodeid every time a dashboard is enrolled,
so a nodeid allowlist would go stale on an enrollment instead of on a rename. `addopts` carries
`-m 'not ui and not image'`, so the `pytest` shards never reach this branch. The `image`
marker takes the same exemption and is run by `image-leg.yml` alone.

## The textfile directory points at a temp directory

`homelab-check-lib.sh`'s `write_check_textfile` writes `$TEXTFILE_DIR/<check>.prom` and
defaults to `/var/lib/node-exporter-textfile`, which node_exporter scrapes. On daniel-server
the deploy user can write there, so a test that ran a check script published its fake verdict
as a production metric (#6191). `pytest_configure` sets `TEXTFILE_DIR` to a directory under the
stub directory, overriding any exported value, so every child that inherits this process's
environment writes there.

A child given an environment built from scratch inherits nothing, so the plugin also wraps
`subprocess.Popen.__init__`. Every launch that passes `env=` gets each `CARRIED_VARS` entry
it does not set itself: `TEXTFILE_DIR`, and `LEAKGUARD_CALLS` so a shim the child reaches
records the call. Two tests once wrote fake verdicts into the real directory that way
(#6191, #6238). The wrapper covers `subprocess.run`, `Popen`, `lib.proc_testing.run` and
asyncio's subprocesses alike, because all of them construct a `Popen`. A test that sets its
own `TEXTFILE_DIR` keeps it.

The wrapper also shims a hand-built `PATH`, such as `/usr/bin:/bin`, which would otherwise
reach the real `logger`, `curl` or `kubectl` with nothing recorded (#6418). It prepends a
directory holding a shim for each binary whose first match on the child's `PATH` is the real
binary, and for nothing else. A test's own stub earlier on that `PATH` still wins, and a binary
the `PATH` hides stays hidden. The `DECIDED:` marker at the wrapper has the reasoning.

A child started by absolute path, such as `/usr/bin/logger`, never consults `PATH` at all. The
wrapper swaps such a program for its shim when it names a shimmed binary in one of the
directories the real `PATH` lists, whether it arrives as `argv[0]` or as `executable` (#6463).
A program in any other directory, such as a test's own stub, runs as given. Two launches stay
uncovered: a `shell=True` command string, and an absolute path a script hardcodes and runs
itself, because the wrapper sees only the direct child.

## Ansible's local temp directory points at a temp directory

Importing ansible-core's constants creates `ansible-local-<pid><8 chars>` under
`~/.ansible/tmp` and registers only an atexit handler to remove it. A pytest run killed by a
signal runs no atexit handler, so it leaves one empty directory per process that imported
ansible: one per xdist worker, within a fraction of a second, with consecutive pids. That is
the shape found on daniel-box after #4893 (#5904). A harness that kills a background
`git commit` when its session ends kills prek's pytest hooks with it, and a timed-out Bash
call kills a run the same way.

At import, before any conftest can import ansible and before xdist spawns its workers, this
plugin sets `ANSIBLE_LOCAL_TEMP` to `ANSIBLE_LOCAL_TEMP_DIR`, overriding any exported value.
That directory sits under the system temp directory and carries the uid in its name, so two
users' runs never share it. Each process that loads the plugin also removes every
`ansible-local-<pid>*` there whose process is gone, so a killed run's directories last only
until the next run starts. The dead-pid test is what makes the sweep safe while other
sessions' runs are live in the same directory.

## What is deliberately NOT shimmed

`git` — too central to intercept safely. Every write verb targets a `tmp_path` repo through `git -C`, no child environment
carries `GIT_DIR`/`GIT_INDEX_FILE`, and every `clone`/`fetch` names a local `tmp_path` origin.

## The git hook variables are stripped, not shimmed

`git commit` exports `GIT_DIR` and `GIT_INDEX_FILE` to its hooks, and git resolves those
before `-C` or `cwd`. So a test that `prek`'s pytest hook runs, and that runs `git -C
<tmp_path> commit`, writes the REAL repository. This plugin strips them once, for every test,
at load. `GIT_HOOK_VARS` is the set and `strip_env` the seam `test_leakguard.py`
drives.

## The agent user's profile variables are stripped the same way

The `claude` agent user's login profile exports `RUN_HOOK_PROJECT_DIR` as its own clone, and
`scripts/dev/fanout_lib/target.py` reads it into `SERVER_CHECKOUT` at import. The fan-out tests
assert the primary checkout, so run as the agent user they went red while CI
stayed green (#3629). A fixture cannot help, because the constant is computed at collection,
before any fixture runs. `AGENT_PROFILE_VARS` is the set, stripped at load with the git hook
variables, so every test reads the operator's defaults whichever user runs the suite. A test
that wants the agent user's value passes it explicitly, as `test_fanout_target.py` does.

A test that BUILDS a scratch repository needs two things this plugin cannot supply: a commit
identity, and `GIT_CONFIG_GLOBAL` pointed at the null device so this host's global SSH commit
signing stays out. `lib.git_testing` is that helper, and the
`tests-take-the-git-scrub-from-git-testing` row of
`scripts/tests/test_census_rows_test_modules.py` refuses a hand-rolled one.

`ansible-playbook` — six tests in `ansible/tests/longhorn/` deliberately run a real play
against `localhost`, and a stub returning a fixed exit code fails all six. The shared fact
cache those harnesses once fenced off is gone: ansible.cfg keeps none since #3274.
"""

import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

# Every binary here leaves the process: it talks to a daemon, a cluster, a remote host, or an
# API. A test needing one stubs it itself; this catches the ones that forgot.
SHIMMED_BINARIES = (
    "at",
    "busctl",
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
    "systemd-run",
    "wget",
)

# Tests that run the role's own argv against a live API server on purpose. Each is documented
# in its module docstring and already guarded by `skipif(shutil.which("kubectl") is None)`
# plus a "no reachable cluster" skip, so they skip rather than fail where there is no cluster.
# They run with the real PATH: a stub would leave them green while checking nothing.
_LIVE_API_TESTS = frozenset(
    {
        "ansible/tests/longhorn/test_longhorn_api.py::test_the_resolve_returns_a_pod_ip_on_this_node",
        "ansible/tests/longhorn/test_volume_revert.py::test_the_listing_jsonpath_parses",
        "ansible/tests/longhorn/test_volume_snapshot.py::test_the_listing_fields_exist_on_a_real_snapshot",
        "ansible/tests/longhorn/test_volume_snapshot.py::test_the_listing_jsonpath_parses",
        "ansible/tests/longhorn/test_volume_snapshot_cap_headroom.py::test_the_gates_reads_parse_and_return_the_field_it_sums",
        # Not a cluster read: it starts and stops a real systemd user unit, because issue #3160
        # is about what that stop kills. Skipped where no user manager answers.
        "scripts/deploy_tools/tests/test_land_detach.py::test_stopping_the_callers_unit_leaves_the_landing_running_to_its_verdict",
        # Not a cluster read either: they `docker run` crowdsec's sidecar modules in the pinned
        # images, which is the linkage the operator's ruling on #5655 asked to check. They
        # skip where docker is missing and CI is unset.
        "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py::test_the_appsec_verify_runs_in_the_jobs_image_against_a_stub_cscli",
        "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py::test_the_engines_cscli_runs_in_the_jobs_image",
        "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py::test_the_home_allowlist_runs_in_the_jobs_image_against_stub_tools",
        "ansible/roles/k8s/crowdsec/tests/test_jobs_image.py::test_the_lapi_register_init_runs_against_a_fresh_database_with_no_lapi",
    }
)

# The marker `scripts/diagnostics/tests/test_ui_smoke.py` carries, whose tests drive a live
# browser and read SOPS on purpose. Named here rather than spelled inline so the non-vacuity
# test can assert that module still carries it — see the module docstring.
_LIVE_MARKER = "ui"

# The marker on tests that run a script inside a pinned container image with `docker run`
# (ansible/tests/longhorn/test_longhorn_restore_drill_image.py). They reach the Docker socket
# and pull from a registry on purpose. `addopts` deselects them from the `pytest` shards,
# which have no Docker daemon, and `.github/workflows/image-leg.yml` runs them.
_IMAGE_MARKER = "image"


def _runs_against_live_infra(item: pytest.Item) -> bool:
    """True for a test deliberately handed the real PATH — see the module docstring."""
    return (
        item.nodeid in _LIVE_API_TESTS
        or item.get_closest_marker(_LIVE_MARKER) is not None
        or item.get_closest_marker(_IMAGE_MARKER) is not None
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

# What `git commit` exports to its hooks. Each one redirects every git call in the process,
# including one aimed at a `tmp_path` repo with `-C`, at the committing repository.
GIT_HOOK_VARS = frozenset({"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"})


# What the agent user's login profile exports and a module reads at import. Each one swaps an
# operator path that a test asserts for the agent user's own.
AGENT_PROFILE_VARS = frozenset({"RUN_HOOK_PROJECT_DIR"})


def strip_env(environ, names) -> list[str]:
    """Remove every one of `names` from `environ`; returns the names removed, sorted."""
    removed = sorted(name for name in names if name in environ)
    for name in removed:
        del environ[name]
    return removed


# What the `Popen` wrapper copies from this process into an explicit child `env` that lacks
# them. `pytest_configure` sets both, so a child built from scratch still writes its textfile
# under the stub directory and still records a shim call.
CARRIED_VARS = ("TEXTFILE_DIR", "LEAKGUARD_CALLS")


def carry_vars(env, environ) -> dict[str, str]:
    """`env` as a new dict, plus each `CARRIED_VARS` entry `environ` has and `env` lacks."""
    carried = dict(env)
    for name in CARRIED_VARS:
        if name in environ:
            carried.setdefault(name, environ[name])
    return carried


def names_reaching_real(child_path: str, real_binaries) -> list[str]:
    """The names in `real_binaries` whose first match on `child_path` is the real binary.

    `real_binaries` maps a name to `_in_real_dir` of the binary this process would run. A name
    that resolves to anything else, such as a test's own stub, or to nothing, is left out.
    Returns them sorted.
    """
    return sorted(
        name
        for name, real in real_binaries.items()
        if (found := shutil.which(name, path=child_path)) is not None
        and _in_real_dir(found) == real
    )


def _in_real_dir(found: str) -> str:
    """`found` with its directory resolved and its own name kept.

    Resolving the directory makes `/bin/curl` equal `/usr/bin/curl` on a merged-usr host.
    Keeping the name unresolved makes a test's own symlink to the real binary, in a directory
    the test owns, count as the test's stub: it chose to run the real thing.
    """
    return os.path.join(
        os.path.realpath(os.path.dirname(found)), os.path.basename(found)
    )


ANSIBLE_LOCAL_TEMP_DIR = (
    Path(tempfile.gettempdir()) / f"pytest-ansible-local-{os.getuid()}"
)

# `tempfile.mkdtemp(prefix="ansible-local-%s" % os.getpid())` appends 8 random characters.
_ANSIBLE_LOCAL = re.compile(r"ansible-local-(\d+)[a-z0-9_]{8}")


def sweep_dead_ansible_local(parent: Path) -> list[str]:
    """Remove each `ansible-local-<pid>*` in `parent` whose process is gone.

    Returns the names removed, sorted. Any other entry, and the directory of a live process,
    is left alone. Never raises: a test run must not fail over housekeeping.
    """
    removed = []
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        return removed
    for entry in entries:
        match = _ANSIBLE_LOCAL.fullmatch(entry.name)
        if match is None or Path(f"/proc/{match.group(1)}").exists():
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry.name)
    return removed


def use_ansible_local_temp(environ, parent: Path) -> None:
    """Point ansible-core's local temp directory at `parent`, after sweeping it."""
    parent.mkdir(mode=0o700, exist_ok=True)
    sweep_dead_ansible_local(parent)
    environ["ANSIBLE_LOCAL_TEMP"] = str(parent)


# At import rather than in a hook: the controller strips before xdist spawns its workers, each
# worker strips again as it loads the plugin, and no session fixture runs first. The same
# holds for ansible's temp directory, which a conftest's ansible import would create.
strip_env(os.environ, GIT_HOOK_VARS | AGENT_PROFILE_VARS)
use_ansible_local_temp(os.environ, ANSIBLE_LOCAL_TEMP_DIR)


def _is_exempt() -> bool:
    """True while the running test is one `_runs_against_live_infra` exempted.

    `is True` rather than a truthiness test: an absent key means no test owns the process, and
    the guard must stay on there.
    """
    return _state.get("exempt") is True


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


def _write_shim(shim: Path) -> None:
    """Write one shim, renaming it into place so a concurrent exec never sees a partial file."""
    partial = shim.with_name(f".{shim.name}.{os.getpid()}")
    partial.write_text(_SHIM)
    partial.chmod(0o755)
    partial.replace(shim)


_STUB_PREFIX = "leakguard-"


def _is_stub_dir(entry: str) -> bool:
    """True for a leakguard stub directory, or a `subset/<names>` shim directory inside one.

    The root is recognised by its `mkdtemp` name AND the `calls` file `pytest_configure`
    writes there. A name match alone would also catch a checkout whose path merely contains a
    `leakguard-` directory, and drop its `.venv/bin` from the PATH every test runs with.
    """
    root = Path(entry)
    if root.parent.name == "subset":
        root = root.parent.parent
    return root.name.startswith(_STUB_PREFIX) and (root / "calls").is_file()


def without_stub_dirs(path: str) -> str:
    """`path` without any leakguard stub directory, an outer run's included.

    `test_leakguard.py` starts a nested pytest with a hand-built PATH, which the outer run's
    wrapper shims. Kept, those entries would make the nested run's exempt tests reach the outer
    shims, and its real binaries resolve to them. Matched by `_is_stub_dir` rather than by the
    temp directory, because the nested run need not share the outer run's `TMPDIR`.
    """
    return os.pathsep.join(
        entry for entry in path.split(os.pathsep) if not _is_stub_dir(entry)
    )


def _shim_dir_for(names: list[str]) -> Path:
    """A directory under the stub directory holding a shim for exactly `names`, made once."""
    stub_dir = _state["stub_dir"]
    assert isinstance(stub_dir, str)
    subset = Path(stub_dir) / "subset" / "+".join(names)
    subset.mkdir(parents=True, exist_ok=True)
    for name in names:
        if not (subset / name).exists():
            _write_shim(subset / name)
    return subset


def shim_hand_built_path(env: dict[str, str]) -> dict[str, str]:
    """`env` with a shim directory before its `PATH` for each name it would run for real.

    An `env` without `PATH` is read as `os.defpath`, the path a child resolves through then.
    Returns `env` itself when no name would reach the real binary.
    """
    real_binaries = _state.get("real_binaries")
    if not isinstance(real_binaries, dict):
        return env
    child_path = env.get("PATH", os.defpath)
    names = names_reaching_real(child_path, real_binaries)
    if not names:
        return env
    return {**env, "PATH": f"{_shim_dir_for(names)}:{child_path}"}


def shim_for_program(program, real_binaries, real_dirs, stub_dir: str) -> str | None:
    """The shim to run instead of `program`, or None to run `program` as given.

    `program` gets a shim only when it is an absolute path whose name is a key of
    `real_binaries` and whose directory, resolved, is one of `real_dirs`. A relative name goes
    through `PATH`, where the PATH prefix and `shim_hand_built_path` already cover it.
    """
    try:
        path = os.fsdecode(program)
    except TypeError:
        return None
    if not os.path.isabs(path):
        return None
    name = os.path.basename(path)
    if name not in real_binaries:
        return None
    if os.path.realpath(os.path.dirname(path)) not in real_dirs:
        return None
    return os.path.join(stub_dir, name)


def shim_absolute_program(args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """`Popen`'s `args` and `kwargs` with an absolute real program swapped for its shim.

    With `executable` set, that is the program the kernel runs and `argv[0]` is only the name
    the child sees, so `executable` alone is swapped. A `shell=True` launch is left alone: its
    program is the shell, and the command string is not parsed here.
    """
    real_binaries = _state.get("real_binaries")
    real_dirs = _state.get("real_dirs")
    stub_dir = _state.get("stub_dir")
    if (
        not isinstance(real_binaries, dict)
        or not isinstance(real_dirs, frozenset)
        or not isinstance(stub_dir, str)
        or kwargs.get("shell")
    ):
        return args, kwargs
    if kwargs.get("executable") is not None:
        shim = shim_for_program(
            kwargs["executable"], real_binaries, real_dirs, stub_dir
        )
        return (
            (args, kwargs) if shim is None else (args, {**kwargs, "executable": shim})
        )
    if args:
        argv = args[0]
    elif "args" in kwargs:
        argv = kwargs["args"]
    else:
        return args, kwargs
    if isinstance(argv, (str, bytes, os.PathLike)):
        shim = shim_for_program(argv, real_binaries, real_dirs, stub_dir)
        swapped = shim
    elif argv:
        argv = list(argv)
        shim = shim_for_program(argv[0], real_binaries, real_dirs, stub_dir)
        swapped = [shim, *argv[1:]]
    else:
        return args, kwargs
    if shim is None:
        return args, kwargs
    if args:
        return (swapped, *args[1:]), kwargs
    return args, {**kwargs, "args": swapped}


def _install_shims(stub_dir: Path, real_path: str) -> list[str]:
    """Write a recording shim for each SHIMMED_BINARIES entry the host actually has.

    Returns the names shimmed. A binary the host lacks is skipped so that a
    `skipif(shutil.which(...) is None)` guard keeps skipping — see the module docstring.
    """
    installed = []
    for name in SHIMMED_BINARIES:
        if shutil.which(name, path=real_path) is None:
            continue
        _write_shim(stub_dir / name)
        installed.append(name)
    return installed


def pytest_configure(config: pytest.Config) -> None:
    stub_dir = Path(tempfile.mkdtemp(prefix=_STUB_PREFIX))
    calls = stub_dir / "calls"
    calls.touch()

    _state["stub_dir"] = str(stub_dir)
    _state["calls"] = calls
    _state["real_path"] = real_path = without_stub_dirs(os.environ.get("PATH", ""))
    _state["installed"] = installed = _install_shims(stub_dir, real_path)
    _state["real_binaries"] = {
        name: _in_real_dir(found)
        for name in installed
        if (found := shutil.which(name, path=real_path)) is not None
    }
    _state["real_dirs"] = frozenset(
        os.path.realpath(entry) for entry in real_path.split(os.pathsep) if entry
    )

    os.environ["LEAKGUARD_CALLS"] = str(calls)
    textfile_dir = stub_dir / "textfile"
    textfile_dir.mkdir()
    os.environ["TEXTFILE_DIR"] = str(textfile_dir)

    real_popen_init = subprocess.Popen.__init__

    # DECIDED: the wrapper shims a hand-built PATH per binary, never by prepending the whole
    # stub directory. That would put every shim in FRONT of the test's own stub directory, so
    # a test's stub `curl` would lose to the shim, and a test that pins `/usr/bin:/bin` to hide
    # a binary would find a shim instead. `shim_hand_built_path` shims only a name whose first
    # match on the child's PATH is already the real binary, so neither case changes (#6418).
    # It runs only while a non-exempt test owns the process, as the PATH prefix in
    # `pytest_runtest_setup` does. `shim_absolute_program` follows the same rule for a child
    # started by absolute path: only a program in a real PATH directory is swapped (#6463).

    def carrying_popen_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Every caller in this repo passes `env` by keyword; a positional one is not covered.
        if kwargs.get("env") is not None:
            kwargs["env"] = carry_vars(kwargs["env"], os.environ)
            if _state.get("exempt") is False:
                kwargs["env"] = shim_hand_built_path(kwargs["env"])
        if _state.get("exempt") is False:
            args, kwargs = shim_absolute_program(args, kwargs)
        real_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = carrying_popen_init  # type: ignore[method-assign]

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        if not _is_loopback(address) and not _is_exempt():
            _record(f"socket connect {address!r}")
            raise RuntimeError(f"LEAKGUARD: blocked a network connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):  # type: ignore[no-untyped-def]
        if not _is_loopback(address) and not _is_exempt():
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
    # One decision, both probes. See the module docstring for why teardown does not clear it.
    exempt = _runs_against_live_infra(item)
    _state["exempt"] = exempt
    os.environ["PATH"] = real_path if exempt else f"{stub_dir}:{real_path}"


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
