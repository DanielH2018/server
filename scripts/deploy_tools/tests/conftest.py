"""Shared fixtures for the deploy-tools tests.

Keeps `land.sh`'s landing annotations out of the real syslog (see `_no_syslog`), and
carries the fakes every land_lib phase test drives.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from deploy_tools.land_lib import landing as landing_mod
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.tools import Tools

# The stub records one line per call so a test can assert it intercepted something. Without
# that record the fixture would be indistinguishable from one that silently stopped being on
# PATH, which is the failure mode `test_land_annotation_is_intercepted` exists to catch.
_LOGGER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$LAND_TEST_LOGGER_CALLS"
"""


@pytest.fixture(autouse=True)
def _no_syslog(tmp_path_factory, monkeypatch):
    """Put a fake `logger` on PATH so a test run writes nothing to syslog.

    `land.sh` emits one logfmt line per landing through `logger`, from an EXIT trap that fires
    on every verdict including the `die` paths. Three test modules here run the real script
    against a stubbed `gh`, so every one of those runs reached the host's syslog, shipped to
    Loki, and landed on the Landings dashboard beside real landings.

    Measured over the two days to 2026-09-03: 2,169 of the 2,577 landing annotations in Loki
    were fixtures — 84% of the board. They are not merely extra rows. They carry `pr=999`,
    `pr=939` and `pr=unknown` with `verdict=aborted`, so any group-by over the dashboard
    reports a landing failure rate dominated by tests that passed.

    Autouse and directory-wide rather than opt-in per test: the three modules that run
    `land.sh` today are not a closed set, and a fourth added later would silently start
    polluting again. A stubbed `logger` no test calls costs nothing.

    The three modules build their subprocess env from `os.environ` with their own stub dir
    prepended, so mutating PATH here is inherited by all of them: their `gh` stub still wins
    for `gh`, and this wins for `logger` over `/usr/bin/logger`.
    """
    stub_dir = tmp_path_factory.mktemp("logger-stub")
    logger = stub_dir / "logger"
    logger.write_text(_LOGGER_STUB)
    logger.chmod(0o755)

    calls = stub_dir / "logger-calls"
    calls.touch()
    monkeypatch.setenv("LAND_TEST_LOGGER_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    return calls


@pytest.fixture
def logger_calls(_no_syslog) -> Path:
    """The file the stubbed `logger` appends to, one line per call."""
    return _no_syslog


MERGE_SHA = "0123456789abcdef0123456789abcdef01234567"
PRIMARY = Path("/primary")
STATE = Path("/state")


def _cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


@dataclass
class Fakes:
    """What each fake answers; every field is a per-test override.

    A list is consumed one entry per call, the last entry repeating.
    """

    gh_views: dict[str, Any] = field(default_factory=dict)
    gh_merge_rc: int = 0
    fetch_rc: int = 0
    pull_ref_rc: int = 0
    tip: str = MERGE_SHA
    await_ci: list[tuple[int, str]] = field(default_factory=lambda: [(0, "CI green")])
    tick: list[int] = field(default_factory=lambda: [0])
    deploy: list[int] = field(default_factory=lambda: [0])
    blockers: list[int] = field(default_factory=lambda: [0])
    hosts: str = ""
    hosts_rc: int = 0
    changed: str = ""
    changed_rc: int = 0
    gate: tuple[bool, list[str]] = (True, ["sonarr: healthy"])
    plane: str = ""
    self_applied: bool = False
    derived: tuple[list[str], str] = (["sonarr"], "pr")
    state: dict[str, str] = field(default_factory=dict)
    lock_holder: str = "42 flock deploy"
    hostname: str = "daniel-box"


def _seq(values: list, calls: list, name: str):
    it = iter(values)
    last = values[-1]

    def answer(*args, **kwargs):
        nonlocal last
        calls.append((name, args, kwargs))
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return answer


def build_tools(f: Fakes) -> tuple[Tools, list]:
    calls: list = []
    views = {
        k: (list(v) if isinstance(v, list) else [v]) for k, v in f.gh_views.items()
    }
    views.setdefault("mergeCommit", [{"mergeCommit": {"oid": MERGE_SHA}}])
    views.setdefault(
        "files,changedFiles",
        [
            {
                "files": [{"path": "ansible/roles/k8s/sonarr/defaults/main.yml"}],
                "changedFiles": 1,
            }
        ],
    )
    view_seq = {k: _seq(v, calls, f"gh:{k}") for k, v in views.items()}

    def gh_json(*args, **kwargs):
        return view_seq[args[args.index("--json") + 1]]()

    def gh_run(*args, **kwargs):
        calls.append(("gh", args, kwargs))
        if f.gh_merge_rc:
            raise subprocess.CalledProcessError(f.gh_merge_rc, args, stderr="boom")
        return _cp()

    def git_run(*args, cwd=None, check=True, **kwargs):
        calls.append(("git", args, {"cwd": cwd}))
        if args[0] == "fetch" and "refs/pull" in args[-1]:
            return _cp(f.pull_ref_rc)
        if args[0] == "fetch":
            return _cp(f.fetch_rc)
        if args == ("rev-parse", "FETCH_HEAD"):
            return _cp(0, "prhead\n")
        if args[0] == "merge-base":
            return _cp(0, "prbase\n")
        if args == ("rev-parse", f"origin/{landing_mod.BRANCH}"):
            return _cp(0, f.tip + "\n")
        return _cp()

    blockers = _seq(f.blockers, [], "")

    def deploy_tags(primary: Path, args: list[str]):
        calls.append(("deploy_tags", tuple(args), {"cwd": primary}))
        if args[0] == "blockers":
            return _cp(blockers())
        if args[0] == "hosts":
            return _cp(f.hosts_rc, f.hosts)
        if args[0] == "changed":
            return _cp(f.changed_rc, f.changed)
        raise AssertionError(args)

    def gate(tags):
        calls.append(("gate", (tags,), {}))
        return f.gate

    t = [0.0]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    tools = Tools(
        gh_json=gh_json,
        gh=gh_run,
        git=git_run,
        await_ci=_seq(f.await_ci, calls, "await_ci"),
        tick=_seq(f.tick, calls, "tick"),
        deploy=_seq(f.deploy, calls, "deploy"),
        deploy_tags=deploy_tags,
        gate=gate,
        plane_note=lambda paths, quiet=(): f.plane,
        self_applied=lambda paths, quiet=(): f.self_applied,
        derive=lambda paths, changed: f.derived,
        quiet_paths=lambda paths, range_: set(),
        read_state=lambda root, name: f.state.get(name, ""),
        lock_holder=lambda: f.lock_holder,
        hostname=lambda: f.hostname,
        logger=lambda line: calls.append(("logger", (line,), {})),
        sleep=lambda s: calls.append(("sleep", (s,), {})),
        clock=clock,
    )
    return tools, calls


def make_landing(
    fakes: Fakes | None = None, **opts
) -> tuple[landing_mod.Landing, list]:
    """A Landing over fakes, for driving one phase directly."""
    tools, calls = build_tools(fakes or Fakes())
    o = Options(pr="999", primary=PRIMARY, deployer_state=STATE, **opts)
    return landing_mod.Landing(o, tools), calls


@pytest.fixture
def landing():
    return make_landing


@pytest.fixture
def land_run(capsys):
    """Run `land.main(argv)` against Fakes; (rc, stdout, stderr, calls, logline)."""
    # land.py doesn't exist until Task 5; this fixture is unused (and this import unreached)
    # until then.
    import land  # ty: ignore[unresolved-import]

    def run(argv: list[str], fakes: Fakes | None = None):
        tools, calls = build_tools(fakes or Fakes())
        if "--pr" not in argv:
            argv = [*argv, "--pr", "999"]
        rc = land.main(argv, tools=tools)
        cap = capsys.readouterr()
        logline = next((c[1][0] for c in calls if c[0] == "logger"), "")
        return rc, cap.out, cap.err, calls, logline

    return run
