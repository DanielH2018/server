"""The git-tree lock wait outlasts the longest hold of any job that takes the lock (#8665).

A waiter that gives up early fires its unit's `OnFailure=` and skips its run, so a wait shorter
than some holder's hold pages for ordinary contention. Each holder's bound is read from its
RENDERED template:

- a unit that wraps `flock` around its `ExecStart` holds for at most its `TimeoutStartSec`;
- a script that calls `take_git_tree_lock` holds until it exits, so its bound is the sum of the
  `timeout N` steps after the `flock -w` line plus `build_docs.time_budget` for each
  `build_docs.py` run there. Runs on exclusive branches are summed too, which over-counts but
  never under-counts.

The wait must also exceed the longest bound by `HEADROOM_S`, for the steps no budget covers:
`uv` start-up and publish_pr's git and gh calls, each bounded per call.

The holders come from `tree_lock_holders`, the generator behind the docs' holder table, so a new
holder joins this check without an edit here.

Run: uv run pytest hosts/tests/test_tree_lock_wait_covers_holders.py
"""

import re
from functools import cache

import pytest
from _setup_render import role_context
from _shell_render import rendered_shell_text
from build_docs import time_budget
from fragments_lib.fragments_deploy import tree_lock_holders
from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib.environment import render_file
from renderer.renderer_lib.host_sources import confined_env

KNOWN_HOLDERS = frozenset({"homelab-checkout-sync.service", "docs-refresh", "eval-run"})
HEADROOM_S = 600

_FLOCK_WAIT = re.compile(r"\bflock -w (\d+) 9\b")
_TIMEOUT_STEP = re.compile(r"(?:^|[\s(])timeout (\d+) ")
_BUILD_DOCS_RUN = re.compile(r"\bpython scripts/docs/build_docs\.py\b")
_SYSTEMD_UNITS = {"s": 1, "min": 60, "m": 60, "h": 3600}


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def lock_wait(script: str) -> int:
    """The `flock -w` seconds in a rendered script that takes the lock through the macro."""
    waits = [int(m) for line in _code_lines(script) for m in _FLOCK_WAIT.findall(line)]
    assert len(waits) == 1, f"expected one `flock -w N 9` line, found {waits}"
    return waits[0]


def script_hold_bound(script: str) -> int:
    """The summed time budgets of the steps after the lock is taken, failing when there are none.

    A script with no timed step under the lock has no bound to derive the wait from.
    """
    lines = _code_lines(script)
    taken = next(i for i, line in enumerate(lines) if _FLOCK_WAIT.search(line))
    steps = []
    for line in lines[taken + 1 :]:
        steps += [int(m) for m in _TIMEOUT_STEP.findall(line)]
        if _BUILD_DOCS_RUN.search(line):
            steps.append(time_budget(generators="--skip-generators" not in line))
    assert steps, "no timed step after the lock: the hold has no bound"
    return sum(steps)


def unit_hold_bound(unit: str) -> int:
    """A unit's `TimeoutStartSec` in seconds, failing when the unit sets none."""
    values = re.findall(r"^TimeoutStartSec=(\S+)$", unit, re.M)
    assert values, "no TimeoutStartSec: a oneshot unit's hold has no bound"
    match = re.fullmatch(r"(\d+)(s|min|m|h)?", values[-1])
    assert match, f"unparsed TimeoutStartSec={values[-1]}"
    return int(match[1]) * _SYSTEMD_UNITS[match[2] or "s"]


def _rendered(source: str) -> str:
    path = REPO / source
    if path.parent.name == "templates":
        return rendered_shell_text("components", path.parents[1].name, path.name)
    component = path.parents[1]
    spec = yaml_fast.safe_load((component / "component.yml").read_text())
    return render_file(
        confined_env(component),
        path.relative_to(component).as_posix(),
        role_context(component, spec.get("defaults")),
    )


@cache
def _holders() -> tuple[tuple[str, str, int], ...]:
    """(job, rendered text, hold bound in seconds) for every tree-lock holder."""
    holders = []
    for holder in tree_lock_holders():
        text = _rendered(holder["source"])
        is_unit = holder["source"].endswith(".service.j2")
        bound = unit_hold_bound(text) if is_unit else script_hold_bound(text)
        holders.append((holder["job"], text, bound))
    return tuple(holders)


def test_every_known_holder_is_found():
    assert KNOWN_HOLDERS <= {job for job, _, _ in _holders()}


def test_every_waiter_outlasts_the_longest_hold_with_headroom():
    longest, _, bound = max(_holders(), key=lambda h: h[2])
    waiters = [
        (job, lock_wait(text))
        for job, text, _ in _holders()
        if _FLOCK_WAIT.search(text)
    ]
    assert {"docs-refresh", "eval-run"} <= {job for job, _ in waiters}
    floor = bound + HEADROOM_S
    short = [f"{job} waits {wait}s" for job, wait in waiters if wait < floor]
    assert not short, (
        f"{longest} can hold the git-tree lock for {bound}s, so a waiter needs {floor}s "
        f"with headroom, but {', '.join(short)}; "
        "raise git_tree_lock_wait_s in k8s/templates/git-tree-lock.j2"
    )


def test_a_script_with_timed_steps_under_the_lock_is_clean():
    script = (
        "# timeout 9999 in a comment is not a step\n"
        "timeout 5 before-the-lock\n"
        "flock -w 60 9 || exit 1\n"
        'timeout 900 "$UV" run pytest\n'
        'MSG="$(timeout 300 "$UV" run thing)"\n'
        "gh pr create --timeout 50 \n"
        "uv run python scripts/docs/build_docs.py --site-dir /srv\n"
        "uv run python scripts/docs/build_docs.py --skip-generators --site-dir /srv\n"
    )
    assert script_hold_bound(script) == (
        1200 + time_budget() + time_budget(generators=False)
    )
    assert lock_wait(script) == 60


def test_a_script_with_no_timed_step_under_the_lock_is_flagged():
    with pytest.raises(AssertionError, match="no bound"):
        script_hold_bound("flock -w 60 9 || exit 1\ngit push\n")


def test_a_unit_timeout_is_read_in_seconds_and_a_missing_one_is_flagged():
    assert unit_hold_bound("[Service]\nTimeoutStartSec=150\n") == 150
    assert unit_hold_bound("[Service]\nTimeoutStartSec=6h\n") == 21600
    with pytest.raises(AssertionError, match="no bound"):
        unit_hold_bound("[Service]\nType=oneshot\n")
