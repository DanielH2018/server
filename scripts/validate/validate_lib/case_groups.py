"""Run the host package cases' groups concurrently, each in its own scratch directory.

`host_package_cases.main()` hands `run_groups` the case groups, or one shard's share of them:
the three `Cases` methods, each case module's `run`, and `node_package_case.run` once per node.
Every group boots its own containers, and their names differ, but
the serial runner had every container mount the one `<work>/repo` and `<work>/debs`, and every
group republishes that repository. So `isolate` gives each group a shallow copy of `Cases` whose
`work` is `<work>/groups/<name>`, with its own `repo/`, `build/` and a copy of each prepared
`.deb`, and its own `debs`, `versions`, `passed`, `failed`, `no_verdict` and `case_seconds`.
`secrets_case.seal_pi` rewrites `debs` labels such as `pi-head`, so a group never sees another
group's sealed build.

Each group's block ends with a `[DONE]` line holding its wall time. A group that ran more than
one case through `Cases.case`, such as `upgrade_path`'s chain, also names its three slowest
cases with their seconds there, because its output reaches the log only when it ends and the
log's timestamps cannot time a case.

Groups run on threads, not processes, so a group looked up at call time is the one that runs.
While they run, `sys.stderr` is a `StderrByThread` that holds each group's output until the
group ends, then writes it as one block. A subprocess that inherits file descriptor 2 bypasses
it, so its output can still land between two blocks.
"""

import copy
import os
import shutil
import sys
import threading
import time
import traceback
from argparse import ArgumentTypeError
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

from lib.progress import count_line

from validate.validate_lib import bootstrap_age_case
from validate.validate_lib import bootstrap_repo_case, disarm_case, engine_case
from validate.validate_lib import (
    conffile_case,
    node_package_case,
    node_units_case,
    repos_case,
    secrets_case,
)


class StderrByThread:
    """A `sys.stderr` that buffers each worker thread's writes and passes the rest through."""

    def __init__(self, real) -> None:
        self.real = real
        self.local = threading.local()
        self.lock = threading.Lock()

    def _buffer(self) -> StringIO | None:
        return getattr(self.local, "buffer", None)

    def write(self, text: str) -> int:
        buffer = self._buffer()
        if buffer is None:
            return self.real.write(text)
        return buffer.write(text)

    def flush(self) -> None:
        if self._buffer() is None:
            self.real.flush()

    def __getattr__(self, name: str):
        return getattr(self.real, name)

    def hold(self) -> None:
        """Start buffering this thread's writes."""
        self.local.buffer = StringIO()

    def release(self) -> None:
        """Write this thread's buffer to the real stream as one block, and stop buffering."""
        buffer = self._buffer()
        self.local.buffer = None
        if buffer is None:
            return
        with self.lock:
            self.real.write(buffer.getvalue())
            self.real.flush()


def _node_group(host: str) -> tuple[str, Callable]:
    return f"node_package_case-{host}", lambda group: node_package_case.run(group, host)


def shard_arg(text: str) -> tuple[int, int]:
    """`--shard K/N` as (K, N), refusing anything but 1 <= K <= N."""
    number, sep, count = text.partition("/")
    if not (sep and number.isdigit() and count.isdigit()):
        raise ArgumentTypeError(f"a shard is K/N, not {text!r}")
    if not 1 <= int(number) <= int(count):
        raise ArgumentTypeError(f"shard {text} is outside 1/{count}..{count}/{count}")
    return int(number), int(count)


def groups(
    cases_class, shard: tuple[int, int] | None = None
) -> list[tuple[str, Callable]]:
    """Every case group but bootstrap_case, longest first, or shard K of N's share of them.

    The order is the groups' wall times on the 26.04 legs of CI runs 46065308899, 46070619651,
    46077939789 and 46082772420, where `node_package_case` ran 340-372 s for both nodes in
    turn; each node is a group of its own, placed by half that. A shard takes every N-th group
    from the K-th, so the longest groups land on different shards.

    `bootstrap_case` is left out: it runs alone under `host_package_cases.py --bootstrap`, in a
    CI job of its own.

    Each is looked up when this runs, so a stub set on a module or on `cases_class` is the one
    the group list holds.
    """
    ordered = [
        ("upgrade_path", cases_class.upgrade_path),
        ("bootstrap_age_case", bootstrap_age_case.run),
        *map(_node_group, node_package_case.NODES),
        ("engine_case", engine_case.run),
        ("disarm", disarm_case.run),
        ("by_hand", cases_class.by_hand),
        ("node_units_case", node_units_case.run),
        ("conffile_case", conffile_case.run),
        ("secrets_case", secrets_case.run),
        ("bootstrap_repo_case", bootstrap_repo_case.run),
        ("repos_case", repos_case.run),
        ("cold_install", cases_class.cold_install),
    ]
    if shard is None:
        return ordered
    number, count = shard
    return ordered[number - 1 :: count]


def isolate(cases, name: str):
    """A copy of `cases` whose scratch directory, debs, versions and verdicts are its own."""
    group = copy.copy(cases)
    group.work = cases.work / "groups" / name
    shutil.rmtree(group.work, ignore_errors=True)
    for directory in ("repo", "debs", "build"):
        (group.work / directory).mkdir(parents=True)
    # Copied, not linked: a later shutil.copy onto a linked name would write every group's file.
    group.debs = {}
    for label, deb in cases.debs.items():
        target = group.work / "debs" / deb.name
        if not target.exists():
            shutil.copy(deb, target)
        group.debs[label] = target
    group.versions = dict(cases.versions)
    group.passed = []
    group.failed = []
    group.no_verdict = []
    group.case_seconds = {}
    return group


def _run_one(
    cases, name: str, run: Callable, stderr: StderrByThread
) -> tuple[list[str], list[str], list[str], float]:
    stderr.hold()
    start = time.monotonic()
    slowest = ""
    try:
        group = isolate(cases, name)
        try:
            run(group)
        except Exception as exc:
            group.failed.append(f"{name}: raised {type(exc).__name__}")
            print(f"[FAIL] {name}: raised outside a case: {exc}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr, end="")
        passed, failed, no_verdict = group.passed, group.failed, group.no_verdict
        slowest = _slowest_cases(group.case_seconds)
    except Exception as exc:
        passed, failed, no_verdict = [], [f"{name}: could not isolate"], []
        print(f"[FAIL] {name}: its scratch directory: {exc}", file=sys.stderr)
    seconds = time.monotonic() - start
    print(f"[DONE] {name} in {seconds:.0f}s{slowest}", file=sys.stderr)
    stderr.release()
    return passed, failed, no_verdict, seconds


def _slowest_cases(case_seconds: dict[str, float]) -> str:
    """The `[DONE]` line's `; slowest cases: <name> <N>s, ...` tail, or "" for one case or none."""
    if len(case_seconds) < 2:
        return ""
    slowest = sorted(case_seconds.items(), key=lambda item: -item[1])[:3]
    return "; slowest cases: " + ", ".join(f"{name} {s:.0f}s" for name, s in slowest)


def run_groups(
    cases, groups: Sequence[tuple[str, Callable]], jobs: int
) -> dict[str, float]:
    """Run each group on a pool of `jobs` threads, and record its verdicts on `cases`.

    Args:
        cases: The prepared `Cases`. Each group gets an `isolate` copy of it.
        groups: `(name, run)` pairs, as `groups` lists them; `run` takes the group's copy and
            records each case on its `passed`, `failed` or `no_verdict`.
        jobs: How many groups run at once.

    Returns:
        Each group's wall time in seconds, by name. `cases.failed` gains every group's
        failures in `groups` order, and a group that raised is one of them. `cases.passed` and
        `cases.no_verdict` gain the passes and the cases that reached no verdict, the same way.
    """
    stderr = StderrByThread(sys.stderr)
    sys.stderr = stderr
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [
                (name, pool.submit(_run_one, cases, name, run, stderr))
                for name, run in groups
            ]
            results = {name: future.result() for name, future in futures}
    finally:
        sys.stderr = stderr.real
    for name, _ in groups:
        passed, failed, no_verdict, _ = results[name]
        cases.passed.extend(passed)
        cases.failed.extend(failed)
        cases.no_verdict.extend(no_verdict)
    slowest = sorted(results.items(), key=lambda item: -item[1][3])
    times = ", ".join(f"{name} {result[3]:.0f}s" for name, result in slowest)
    print(f"group wall times: {times}", file=sys.stderr)
    return {name: result[3] for name, result in results.items()}


# GitHub shows at most 10 error annotations per step, and the runner's exit-code error takes one.
ANNOTATION_CAP = 9


def report(cases, list_passed: bool = False) -> int:
    """Annotate each failed case, print the run's last line, and return the exit status.

    Each failed case gets one `::error` annotation, up to `ANNOTATION_CAP`; past the cap the
    last annotation counts the rest. The last line opens with the count of each verdict and
    lists the failed cases, or the cases with no verdict when none failed. When
    `GITHUB_STEP_SUMMARY` names a file, the count and every failed or no-verdict case are
    appended to it.

    Args:
        cases: The `Cases` whose `passed`, `failed` and `no_verdict` `run_groups` filled.
        list_passed: Print a `[PASS]` line per passing case before the last line.

    Returns:
        1 when a case failed, else 0, even when some cases gave no verdict.
    """
    states = (
        ["failed"] * len(cases.failed)
        + ["no verdict"] * len(cases.no_verdict)
        + ["passed"] * len(cases.passed)
    )
    counts = count_line(states)
    if list_passed:
        for name in cases.passed:
            print(f"[PASS] {name}", file=sys.stderr)
    shown = cases.failed
    if len(shown) > ANNOTATION_CAP:
        shown = shown[: ANNOTATION_CAP - 1]
    for name in shown:
        print(
            f"::error title=host package case failed::{_escape(name)}", file=sys.stderr
        )
    if len(shown) < len(cases.failed):
        rest = len(cases.failed) - len(shown)
        print(
            f"::error title=host package case failed::{rest} more; the job summary lists them",
            file=sys.stderr,
        )
    _summarise(cases, counts)
    if cases.passed and not list_passed:
        counts += "; --all to list the passes"
    if cases.failed:
        print(f"[FAIL] {counts}: {cases.failed}", file=sys.stderr)
        return 1
    if cases.no_verdict:
        print(f"[NO VERDICT] {counts}: {cases.no_verdict}", file=sys.stderr)
        return 0
    print(f"[PASS] {counts}", file=sys.stderr)
    return 0


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _summarise(cases, counts: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["### Host package cases", "", counts, ""]
    lines += [f"- failed: {name}" for name in cases.failed]
    lines += [f"- no verdict: {name}" for name in cases.no_verdict]
    with open(path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")
