#!/usr/bin/env python3
"""Sweep one commit on this host: the checks of CI's gate that need no hosted runner.

A landing waits for hosted runners twice: for its pull request's run, and again for the run
that re-tests the pull request once its branch takes a newer master. A host with idle cores
can take over the second wait (docs/ci-throughput.md, Phase B), and this tool is the check it
runs. It clones the commit into a scratch tree, runs each check of `LOCAL` there and prints
one verdict. It writes nothing outside the scratch tree, which it removes.

`LOCAL` and `HOSTED` together name every job the `prek` gate of `.github/workflows/ci.yml`
needs. A job in `HOSTED` is one this tool does not run, so a `pass` says nothing about it.

The last line is the result a caller reads:

    SWEEP-RESULT: verdict=pass sha=<sha> tree=<tree> seconds=420 hooks=pass hooks_seconds=210 pytest=pass pytest_seconds=205

Exit codes:
  0   every local check passed
  1   a local check failed
  64  usage
  69  no verdict: the commit could not be checked out, a check could not start or outran its
      time limit, or the sweep itself raised

Usage:
    uv run python scripts/deploy_tools/local_sweep.py <commit> [--keep] [--once FILE]

`--keep` leaves the scratch tree and each check's whole log in place, and prints where.

`--once FILE` is for a timer. When FILE already names the commit, the tool prints nothing and
exits 0. Otherwise it writes the commit to FILE before the first check, so a sweep that dies
part-way is not repeated on the next run. A sweep that reaches a verdict then exits 0 whether
the verdict is pass or fail, because a timer's run failed only if the sweep did not happen. That
leaves 1 to mean what Python means by it: the tool itself did not start.
"""

import dataclasses
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib import yaml_fast
from lib.cli_help import HELP_FLAGS
from lib.exit_codes import (
    SWEEP_BAD_ARGS,
    SWEEP_FAILED,
    SWEEP_NO_VERDICT,
    SWEEP_PASSED,
)
from lib.git import git, git_env, git_stdout
from lib.progress import count_line
from lib.repo_paths import REPO

CI_WORKFLOW = Path(".github/workflows/ci.yml")
HOOKS_STEP = "Run hooks (full sweep)"

# The whole suite took 198s and the hooks 87s on daniel-box (2026-11-13). A check still
# running at nine times that is hung, and a caller waiting on the verdict needs a bound.
CHECK_TIMEOUT = 30 * 60

# A failed check lists at most this many of the lines that name what failed.
MAX_DETAIL = 20

_RESULT = "SWEEP-RESULT: "

# A scratch directory's name says whether `--keep` asked for it, which `_reap` never removes.
_SCRATCH = "sweep-"
_KEPT = "kept-"
# Six times the two checks' limits together.
STALE_SECONDS = 6 * 3600

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _hooks(tree: Path) -> list[str]:
    """The hooks job's own sweep command, read from the swept commit so it cannot drift."""
    workflow = yaml_fast.safe_load((tree / CI_WORKFLOW).read_text())
    steps = workflow["jobs"]["hooks"]["steps"]
    for step in steps:
        if step.get("name") == HOOKS_STEP:
            # `bash -e` is how GitHub runs a step that names no shell.
            return ["bash", "-e", "-c", step["run"]]
    raise LookupError(f"{CI_WORKFLOW}'s hooks job has no step named {HOOKS_STEP!r}")


def _pytest(tree: Path) -> list[str]:
    # The six hosted shards partition this one run.
    return ["uv", "run", "python", "-m", "pytest"]


@dataclasses.dataclass(frozen=True)
class Local:
    """A job of the gate that this host runs.

    Attributes:
        command: The argv to run in a tree, given the tree.
        failed: Matches the lines of the check's output that name what failed.
        timeout: Seconds the check may run before it is killed.
    """

    command: Callable[[Path], list[str]]
    failed: re.Pattern[str]
    timeout: int = CHECK_TIMEOUT


LOCAL = {
    "hooks": Local(_hooks, re.compile(r"\.Failed$")),
    "pytest": Local(_pytest, re.compile(r"^(FAILED|ERROR) ")),
}

# Why this host runs none of the gate's other jobs.
HOSTED = {
    "pr_gates": "compares a pull request with its base, and every step skips off one",
    "deck_mod": "installs the pinned Claude CLI from npm, and runs only for a pull request "
    "that touches the mod",
    "lockfile": "verifies each locked action commit against GitHub",
    "flux_roundtrip": "needs the flux CLI at the version ci.yml pins",
    "sops_manifest_type_proof": "builds a kind cluster, which needs Docker",
    "host_packages_build": "needs the nfpm release ci.yml pins",
    "host_packages": "installs the packages in a container of each Ubuntu release",
    "host_packages_bootstrap": "installs the packages in a container of each Ubuntu release",
    "host_tools_arm64": "runs on an arm64 runner",
}


@dataclasses.dataclass(frozen=True)
class Check:
    """How one local check ended.

    Attributes:
        job: The gate job's id in ci.yml.
        verdict: `pass`, `fail`, or `none` for a check that did not start or was killed.
        seconds: How long it ran.
        detail: The lines that name what failed, or why there is no verdict.
    """

    job: str
    verdict: str
    seconds: int
    detail: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Sweep:
    """The result of sweeping one commit.

    Attributes:
        sha: The commit swept.
        tree: Its tree id, which is what a tested-tree record names.
        checks: Each local check, in the order run.
        seconds: The whole sweep, the clone included.
        kept: The scratch directory, when the caller asked to keep it.
    """

    sha: str
    tree: str
    checks: tuple[Check, ...]
    seconds: int
    kept: Path | None = None

    @property
    def verdict(self) -> str:
        """`fail` if any check failed, else `pass` if every check ran and passed, else `none`."""
        verdicts = {check.verdict for check in self.checks}
        return next((v for v in ("fail", "none", "pass") if v in verdicts), "none")

    def result_line(self) -> str:
        """The one machine-readable line, in logfmt after the `SWEEP-RESULT:` prefix."""
        checks = " ".join(
            f"{c.job}={c.verdict} {c.job}_seconds={c.seconds}" for c in self.checks
        )
        return (
            f"{_RESULT}verdict={self.verdict} sha={self.sha} tree={self.tree}"
            f" seconds={self.seconds} {checks}"
        )


def parse_result_line(line: str) -> Sweep | None:
    """The sweep that a `Sweep.result_line` describes, or None when `line` carries none.

    Text ahead of the prefix is ignored, so a journal or syslog line parses as it is. The
    result has no `detail` and no `kept`, which the line does not carry.
    """
    fields = line.partition(_RESULT)[2]
    pairs = dict(field.split("=", 1) for field in fields.split() if "=" in field)
    try:
        checks = tuple(
            Check(
                key.removesuffix("_seconds"),
                pairs[key.removesuffix("_seconds")],
                int(seconds),
            )
            for key, seconds in pairs.items()
            if key.endswith("_seconds")
        )
        sweep = Sweep(pairs["sha"], pairs["tree"], checks, int(pairs["seconds"]))
    except KeyError, ValueError:
        return None
    if any(check.verdict not in EXIT for check in checks):
        return None
    # A line cut short still parses, with a check missing and so another verdict. Only a
    # sweep that writes this exact line back is the one the line describes.
    return sweep if sweep.result_line() == _RESULT + fields.strip() else None


def _work_root() -> Path:
    """Where scratch trees go: the user's cache directory, never `/tmp`.

    The Bash-hook tests read any path under `/tmp` as scratch, so a tree cloned there fails
    `test_a_scratch_symlink_into_the_repo_is_not_scratch` on a commit that is green.
    """
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    root = Path(cache) / "local-sweep"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _reap(root: Path) -> None:
    """Remove the scratch trees that a killed sweep left under `root`.

    A sweep that systemd times out, or that a reboot ends, never reaches its own cleanup. A tree
    older than `STALE_SECONDS` belongs to no running sweep, because a sweep is killed long before.
    """
    for left in root.glob(f"{_SCRATCH}*"):
        if time.time() - left.stat().st_mtime > STALE_SECONDS:
            shutil.rmtree(left, ignore_errors=True)


def _resolve(commit: str, repo: Path) -> str:
    return git_stdout("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=repo)


def _check_env() -> dict[str, str]:
    """This process's environment without git's variables or the virtualenv that runs it.

    `uv run` puts its project's `.venv/bin` first on `PATH`. A check that inherited it would run
    a hook's `python` from the checkout this tool was started in, not from the swept tree.
    """
    env = git_env()
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        env["PATH"] = os.pathsep.join(
            p for p in env["PATH"].split(os.pathsep) if Path(p) != Path(venv) / "bin"
        )
    return env


def _run(job: str, local: Local, tree: Path, log: Path) -> Check:
    start = time.monotonic()

    def ended(verdict: str, *detail: str) -> Check:
        return Check(job, verdict, round(time.monotonic() - start), detail)

    try:
        argv = local.command(tree)
        with log.open("wb") as out:
            # Its own session, so a timeout can kill the workers a check started.
            proc = subprocess.Popen(
                argv,
                cwd=tree,
                env=_check_env(),
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except (LookupError, OSError) as exc:
        return ended("none", f"could not start: {exc}")
    try:
        rc = proc.wait(local.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return ended("none", f"outran its {local.timeout}s limit and was killed")
    if rc == 0:
        return ended("pass")
    lines = _ANSI.sub("", log.read_text(errors="replace")).splitlines()
    return ended(
        "fail", *([ln for ln in lines if local.failed.search(ln)] or lines[-10:])
    )


def sweep(
    commit: str,
    *,
    repo: Path = REPO,
    checks: dict[str, Local] = LOCAL,
    keep: bool = False,
) -> Sweep:
    """Run every check of `checks` on `commit`, in a scratch clone of `repo`.

    Args:
        commit: Any name `repo` resolves to a commit. Its objects must already be in `repo`,
            because the sweep fetches nothing.
        repo: The repository to clone. The clone borrows its object store.
        checks: The checks to run, by gate job id.
        keep: Leave the scratch directory in place and name it in the result.

    Raises:
        subprocess.CalledProcessError: git could not resolve `commit` or check it out.
    """
    start = time.monotonic()
    sha = _resolve(commit, repo)
    tree_id = git_stdout("rev-parse", f"{sha}^{{tree}}", cwd=repo)
    root = _work_root()
    _reap(root)
    work = Path(tempfile.mkdtemp(prefix=_KEPT if keep else _SCRATCH, dir=root))
    try:
        tree = work / "tree"
        git("clone", "--quiet", "--shared", "--no-checkout", str(repo), str(tree))
        git("checkout", "--quiet", "--detach", sha, cwd=tree)
        ran = tuple(
            _run(job, local, tree, work / f"{job}.log") for job, local in checks.items()
        )
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return Sweep(
        sha, tree_id, ran, round(time.monotonic() - start), work if keep else None
    )


def first_time(sha: str, record: Path) -> bool:
    """Whether `record` does not name `sha` yet. When it does not, it does from here on."""
    if record.exists() and record.read_text().strip() == sha:
        return False
    record.write_text(f"{sha}\n")
    return True


def report(result: Sweep) -> list[str]:
    """What a sweep prints: a count, each check that did not pass, then the result line."""
    lines = [count_line(check.verdict for check in result.checks)]
    for check in result.checks:
        if check.verdict == "pass":
            continue
        how = "failed" if check.verdict == "fail" else "gave no verdict"
        lines.append(f"{check.job} {how} after {check.seconds}s:")
        lines += [f"  {line}" for line in check.detail[:MAX_DETAIL]]
        if len(check.detail) > MAX_DETAIL:
            more = len(check.detail) - MAX_DETAIL
            lines.append(f"  and {more} more; --keep leaves the whole log")
    if result.kept:
        lines.append(f"kept {result.kept}, with each check's log beside tree/")
    return [*lines, result.result_line()]


EXIT = {"pass": SWEEP_PASSED, "fail": SWEEP_FAILED, "none": SWEEP_NO_VERDICT}


def main(
    argv: list[str] | None = None,
    *,
    repo: Path = REPO,
    checks: dict[str, Local] = LOCAL,
) -> int:
    """Sweep the commit `argv` names, print the report, and return the exit code.

    Args:
        argv: The command line, without the program name.
        repo: The repository that holds the commit. A test passes a scratch one.
        checks: The checks to run. A test passes stand-ins.
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] and argv[0] in HELP_FLAGS:
        print(__doc__.strip())
        return SWEEP_PASSED
    commits = [arg for arg in argv if arg != "--keep"]
    once = None
    if "--once" in commits:
        at = commits.index("--once")
        once, commits = commits[at + 1 : at + 2], commits[:at] + commits[at + 2 :]
    if len(commits) != 1 or commits[0].startswith("-") or once == []:
        print(__doc__.strip(), file=sys.stderr)
        return SWEEP_BAD_ARGS
    try:
        # Resolved once: `origin/master` can move between the record and the sweep.
        sha = _resolve(commits[0], repo)
        if once and not first_time(sha, Path(once[0])):
            return SWEEP_PASSED
        result = sweep(sha, repo=repo, checks=checks, keep="--keep" in argv)
    except subprocess.CalledProcessError as exc:
        why = (exc.stderr or "").strip().splitlines()[-1:] or [str(exc)]
        print(f"no tree to sweep for {commits[0]}: {why[0]}", file=sys.stderr)
        return SWEEP_NO_VERDICT
    except Exception:
        # Python's own exit code for an uncaught error is 1, which here means a check failed.
        traceback.print_exc()
        return SWEEP_NO_VERDICT
    print("\n".join(report(result)))
    if once and result.verdict != "none":
        return SWEEP_PASSED
    return EXIT[result.verdict]


if __name__ == "__main__":
    sys.exit(main())
