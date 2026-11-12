"""Run the host package cases' groups concurrently, each in its own scratch directory (#7784).

`host_package_cases.main()` hands `run_groups` the eleven case groups: the three `Cases` methods
and each case module's `run`. Every group boots its own containers, and their names differ, but
the serial runner had every container mount the one `<work>/repo` and `<work>/debs`, and every
group republishes that repository. So `isolate` gives each group a shallow copy of `Cases` whose
`work` is `<work>/groups/<name>`, with its own `repo/`, `build/` and a copy of each prepared
`.deb`, and its own `debs`, `versions`, `failed` and `no_verdict`. `secrets_case.seal_pi` rewrites `debs`
labels such as `pi-head`, so a group never sees another group's sealed build.

Groups run on threads, not processes, so a group looked up at call time is the one that runs.
While they run, `sys.stderr` is a `StderrByThread` that holds each group's output until the
group ends, then writes it as one block. A subprocess that inherits file descriptor 2 bypasses
it, so its output can still land between two blocks.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import copy
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

from validate.validate_lib import bootstrap_age_case
from validate.validate_lib import bootstrap_repo_case, disarm_case, engine_case
from validate.validate_lib import conffile_case, repos_case, secrets_case, shadow_case


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


def groups(cases_class) -> list[tuple[str, Callable]]:
    """Every case group but bootstrap_case, longest first by CI run 42575480365's 24.04 leg.

    `bootstrap_case` is left out: it runs alone under `host_package_cases.py --bootstrap`, in a
    CI job of its own (#7819).

    Each is looked up when this runs, so a stub set on a module or on `cases_class` is the one
    the group list holds.
    """
    return [
        ("upgrade_path", cases_class.upgrade_path),
        ("bootstrap_age_case", bootstrap_age_case.run),
        ("engine_case", engine_case.run),
        ("by_hand", cases_class.by_hand),
        ("disarm", disarm_case.run),
        ("conffile_case", conffile_case.run),
        ("secrets_case", secrets_case.run),
        ("bootstrap_repo_case", bootstrap_repo_case.run),
        ("repos_case", repos_case.run),
        ("shadow_case", shadow_case.run),
        ("cold_install", cases_class.cold_install),
    ]


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
    group.failed = []
    group.no_verdict = []
    return group


def _run_one(
    cases, name: str, run: Callable, stderr: StderrByThread
) -> tuple[list[str], list[str], float]:
    stderr.hold()
    start = time.monotonic()
    try:
        group = isolate(cases, name)
        try:
            run(group)
        except Exception as exc:
            group.failed.append(f"{name}: raised {type(exc).__name__}")
            print(f"[FAIL] {name}: raised outside a case: {exc}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr, end="")
        failed, no_verdict = group.failed, group.no_verdict
    except Exception as exc:
        failed, no_verdict = [f"{name}: could not isolate"], []
        print(f"[FAIL] {name}: its scratch directory: {exc}", file=sys.stderr)
    seconds = time.monotonic() - start
    print(f"[DONE] {name} in {seconds:.0f}s", file=sys.stderr)
    stderr.release()
    return failed, no_verdict, seconds


def run_groups(
    cases, groups: Sequence[tuple[str, Callable]], jobs: int
) -> dict[str, float]:
    """Run each group on a pool of `jobs` threads, and record its verdicts on `cases`.

    Args:
        cases: The prepared `Cases`. Each group gets an `isolate` copy of it.
        groups: `(name, run)` pairs, as `groups` lists them; `run` takes the group's copy and
            records each failed case on its `failed`.
        jobs: How many groups run at once.

    Returns:
        Each group's wall time in seconds, by name. `cases.failed` gains every group's
        failures in `groups` order, and a group that raised is one of them. `cases.no_verdict`
        gains every case that could not reach a verdict, the same way.
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
        cases.failed.extend(results[name][0])
        cases.no_verdict.extend(results[name][1])
    slowest = sorted(results.items(), key=lambda item: -item[1][2])
    times = ", ".join(f"{name} {seconds:.0f}s" for name, (_, _, seconds) in slowest)
    print(f"group wall times: {times}", file=sys.stderr)
    return {name: seconds for name, (_, _, seconds) in results.items()}


def verdict(cases) -> tuple[str, int]:
    """The run's last line and exit status: 1 when a case failed, else 0, even with no verdicts."""
    if cases.failed:
        return f"[FAIL] {len(cases.failed)} cases failed: {cases.failed}", 1
    if cases.no_verdict:
        return (
            f"[NO VERDICT] {len(cases.no_verdict)} cases gave no verdict, and every other"
            f" case passed: {cases.no_verdict}",
            0,
        )
    return "every case passed", 0
