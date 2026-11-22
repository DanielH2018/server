"""The CI workflow's pytest job, concurrency group and sweep-size comments, as census rows.

`test_census_rows_workflows.py` runs `CI_JOB_ROWS`. Each row's red half is the real file with
one property broken.
"""

import re
from collections.abc import Callable
from math import prod
from pathlib import PurePosixPath

from _ci_workflow_rows import ci_job, ci_row
from _pytest.assertion.rewrite import PYC_TAIL
from _row_table import Census, Subject, tracked
from _workflow_rows import (
    CI,
    Workflow,
    axis_values,
    checkouts,
    judge,
    jobs,
    load,
    mutants,
    parsed,
    steps,
    uses,
)

PYTEST_JOB = "pytest"
RATCHET = "tests/repo/test_module_length_ratchet.py"
TEST_STEP = "Run the test suite"
RESTORE = "actions/cache/restore"
SAVE = "actions/cache/save"
CACHE = "actions/cache"
SWEEP = ".github/workflows/pytest-sweep.yml"
REF_ONLY_GROUP = "${{ github.workflow }}-${{ github.ref }}"
STALE_PYC_GLOB = "**/__pycache__/*.cpython-315.pyc"
SWEEP_CITATION = re.compile(r"full[\s#]+sweep[\s#]+runs[\s#]+(\d+)[\s#]+jobs")
_SKIPPED_BY = {
    "push": "github.event_name != 'push'",
    "pull_request": "github.event_name == 'push'",
}


def _fetch_depth_problems(wf: Workflow) -> list[str]:
    depths = [
        (s.get("with") or {}).get("fetch-depth")
        for s in checkouts(ci_job(wf, PYTEST_JOB))
    ]
    if depths == [0]:
        return []
    return [f"the {PYTEST_JOB} job's checkouts fetch {depths}, not [0]"]


def _pytest_checkouts(wf: Workflow) -> list[dict]:
    return checkouts(wf["jobs"][PYTEST_JOB])


def _set_fetch_depth(depth: int) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        for step in _pytest_checkouts(wf):
            step["with"]["fetch-depth"] = depth

    return mutate


def _unset_fetch_depth(wf: Workflow) -> None:
    for step in _pytest_checkouts(wf):
        del step["with"]["fetch-depth"]


def _drop_the_checkout(wf: Workflow) -> None:
    job = wf["jobs"][PYTEST_JOB]
    job["steps"] = [s for s in steps(job) if not uses(s, "actions/checkout")]


def _concurrency(wf: Workflow) -> dict:
    found = wf.get("concurrency")
    return found if isinstance(found, dict) else {}


def _push_group_problems(wf: Workflow) -> list[str]:
    group = str(_concurrency(wf).get("group", ""))
    if "github.sha" in group:
        return []
    return [f"concurrency.group is {group!r}, which does not key a push on the commit"]


def _pull_request_group_problems(wf: Workflow) -> list[str]:
    concurrency = _concurrency(wf)
    group = str(concurrency.get("group", ""))
    found = []
    if "github.ref" not in group:
        found.append(
            f"concurrency.group {group!r} does not key a pull request on its ref"
        )
    if "pull_request" not in group:
        found.append(f"concurrency.group {group!r} uses the ref unconditionally")
    if "pull_request" not in str(concurrency.get("cancel-in-progress")):
        found.append("cancel-in-progress is not scoped to pull_request")
    return found


def _set_concurrency(**fields: object) -> Callable[[Workflow], None]:
    return lambda wf: wf["concurrency"].update(fields)


def _matrix_jobs(job: dict) -> int | str:
    """How many jobs one workflow job expands to, or why its matrix cannot be counted."""
    matrix = (job.get("strategy") or {}).get("matrix")
    if matrix is None:
        return 1
    axes = [axis_values(v) for k, v in matrix.items() if k != "include"]
    if "exclude" in matrix or (axes and "include" in matrix):
        return f"unhandled matrix: {matrix}"
    return len(matrix["include"]) if "include" in matrix else prod(len(v) for v in axes)


def _sweep_size(wf: Workflow, event: str) -> int | str:
    counts = [
        _matrix_jobs(job)
        for job in jobs(wf).values()
        if _SKIPPED_BY[event] not in str(job.get("if", ""))
    ]
    bad = [n for n in counts if isinstance(n, str)]
    return bad[0] if bad else sum(n for n in counts if isinstance(n, int))


def _sweep_citation_problems(subject: Subject) -> list[str]:
    wf = parsed(subject)
    push, pull_request = _sweep_size(wf, "push"), _sweep_size(wf, "pull_request")
    if push != pull_request or isinstance(push, str):
        return [f"a push sweep runs {push} jobs, a pull request sweep {pull_request}"]
    found = [int(n) for n in SWEEP_CITATION.findall(subject.text)]
    if found == [push] * 2:
        return []
    return [f"the comments cite sweeps of {found}, not two of {push}"]


def rewrite_step(job: dict, action: str) -> dict | None:
    """The first `action` step of `job` whose path glob matches a rewritten module."""
    rewritten = PurePosixPath("tests/repo/__pycache__/test_x" + PYC_TAIL)
    for step in steps(job):
        path = str((step.get("with") or {}).get("path", ""))
        if uses(step, action) and any(rewritten.full_match(g) for g in path.split()):
            return step
    return None


def _after_tests(wf: Workflow, step: dict) -> bool | None:
    names = [s.get("name") for s in steps(ci_job(wf, PYTEST_JOB))]
    if TEST_STEP not in names:
        return None
    return names.index(step.get("name")) > names.index(TEST_STEP)


def _restore_before_tests_problems(wf: Workflow) -> list[str]:
    step = ci_rewrite_restore(wf)
    if step is None:
        return [f"no {RESTORE} step matches the `*{PYC_TAIL}` files pytest writes"]
    if _after_tests(wf, step) is not False:
        return [f"the restore step does not run before {TEST_STEP!r}"]
    return []


def ci_rewrite_restore(wf: Workflow) -> dict | None:
    return rewrite_step(ci_job(wf, PYTEST_JOB), RESTORE)


def sweep_rewrite_save(wf: Workflow) -> dict | None:
    return rewrite_step(jobs(wf).get("sweep", {}), SAVE)


def rewrite_cache_link_problems(restore: dict | None, save: dict | None) -> list[str]:
    """What keeps ci.yml's rewrite-cache restore from reading the sweep's saved entry."""
    if restore is None or save is None:
        return [
            f"no rewrite-cache restore in {CI}, or no rewrite-cache save in {SWEEP}"
        ]
    found = []
    if restore["with"].get("path") != save["with"].get("path"):
        found.append("the restore path differs from the path the sweep saves")
    prefix, key = (
        str(restore["with"].get("restore-keys") or ""),
        str(save["with"]["key"]),
    )
    if not prefix or not key.startswith(prefix):
        found.append(
            f"restore-keys {prefix!r} is not a prefix of the sweep's key {key!r}"
        )
    return found


def _rewrite_cache_problems(wf: Workflow) -> list[str]:
    job = ci_job(wf, PYTEST_JOB)
    saves = [
        f"a {PYTEST_JOB} leg saves the rewrite cache through {action}"
        for action in (SAVE, CACHE)
        if rewrite_step(job, action) is not None
    ]
    link = rewrite_cache_link_problems(
        ci_rewrite_restore(wf), sweep_rewrite_save(load(SWEEP))
    )
    return saves + link


def _add_leg_save(action: str) -> Callable[[Workflow], None]:
    """A mutation that appends a copy of the restore step that uses `action` instead."""

    def mutate(wf: Workflow) -> None:
        restore = ci_rewrite_restore(wf)
        assert restore is not None
        save = {**restore, "uses": restore["uses"].replace(RESTORE, action)}
        steps(wf["jobs"][PYTEST_JOB]).append(save)

    return mutate


def _edit_restore(**fields: object) -> Callable[[Workflow], None]:
    """A mutation that sets keys of `with` on the pytest job's rewrite-cache restore step."""

    def mutate(wf: Workflow) -> None:
        step = ci_rewrite_restore(wf)
        assert step is not None
        step["with"].update(fields)

    return mutate


def _drop_restore_keys(wf: Workflow) -> None:
    step = ci_rewrite_restore(wf)
    assert step is not None
    step["with"].pop("restore-keys")


def _restore_after_tests(wf: Workflow) -> None:
    job = wf["jobs"][PYTEST_JOB]
    moved = ci_rewrite_restore(wf)
    rest = [s for s in steps(job) if s is not moved]
    at = [s.get("name") for s in rest].index(TEST_STEP) + 1
    job["steps"] = [*rest[:at], moved, *rest[at:]]


def _bare_sweep(comment: str, jobs_block: str) -> Subject:
    return Subject("a.yml", f"{comment}\njobs:\n{jobs_block}")


_TWO_JOBS = "  a:\n    steps: []\n  b:\n    steps: []\n"
_SWEEP_OF_TWO = "# the full sweep runs 2 jobs\n# a full sweep runs 2 jobs\n"

CI_JOB_ROWS = (
    Census(
        **ci_row(
            name="ci-pytest-job-checks-out-full-history",
            reason=(
                "`test_no_allowlist_entry_rose_against_origin_master` in "
                "`test_module_length_ratchet.py` skips when `origin/master` is unresolvable. On "
                "the runner it resolves only because the `pytest` job checks out with "
                "`fetch-depth: 0`. A `fetch-depth: 1`, or an omitted line, is a depth-1 clone: "
                "the anti-raise half of the ratchet becomes a skip with CI still green, which "
                "is the repo's own non-vacuity failure class. This row asserts the workflow "
                "shape instead of failing inside the ratchet on `CI`, because an env-gated "
                "failure there would hardcode an assumption about the environment into a test "
                "that has none. A checkout the parse missed reads as an empty list, so the "
                "job must hold exactly one checkout."
            ),
            offence=judge(_fetch_depth_problems),
            red=mutants(
                CI,
                a_shallow_checkout=_set_fetch_depth(1),
                a_checkout_with_no_fetch_depth=_unset_fetch_depth,
                a_pytest_job_with_no_checkout=_drop_the_checkout,
            ),
        )
    ),
    Census(
        name="ci-history-ratchet-reads-origin-master",
        reason=(
            "`ci-pytest-job-checks-out-full-history` pins a workflow line for one consumer, "
            "the ratchet's anti-raise half. When the ratchet stops comparing against "
            "`origin/master`, the line has no subject, and the check needs re-examining "
            "rather than pinning a line nobody depends on."
        ),
        files=lambda: tracked(RATCHET),
        offence=lambda s: [] if "origin/master" in s.text else ["no `origin/master`"],
        red=(Subject("a.py", "def test_x():\n    assert base == 'main'\n"),),
        green=(Subject("a.py", "BASE = 'origin/master'\n"),),
        must_find=frozenset({RATCHET}),
    ),
    Census(
        **ci_row(
            name="ci-master-push-groups-by-commit",
            reason=(
                "Every master SHA needs its own CI verdict: checkout-sync's CI gate and "
                "`await_ci.py` read check-runs per commit, and a commit with no verdict reads "
                "`cancelled` forever, so every landing behind it waits on a later SHA's full "
                "sweep. `cancel-in-progress: false` does not buy that: it governs the running "
                "run and says nothing about a queued one. GitHub keeps at most one pending run "
                "per concurrency group, so each push evicts the pending run ahead of it when "
                "master moves faster than a sweep, and the evicted run is cancelled with zero "
                "jobs. The property is that no two master pushes share a group. A group keyed "
                "on `github.sha` has it. One keyed on `github.ref` does not, because every "
                "push to master carries the same ref. A workflow with no concurrency mapping "
                "fails too, because a guard over a missing key checks nothing."
            ),
            offence=judge(_push_group_problems),
            red=mutants(
                CI,
                a_group_keyed_on_the_ref=_set_concurrency(group=REF_ONLY_GROUP),
                a_workflow_with_no_concurrency=lambda wf: wf.pop("concurrency"),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-pull-request-groups-by-ref-and-cancels-itself",
            reason=(
                "A pull request groups by ref, so a force-push supersedes its own earlier run. "
                "Grouping a PR by SHA would leave the superseded runs going. The ref arm is "
                "conditional on `pull_request`: a group of `${{ github.workflow }}-${{ "
                "github.ref }}` satisfies the ref check alone, and would send master back to "
                "losing verdicts. `cancel-in-progress` stays scoped to `pull_request`, "
                "because cancelling a master run is the very thing that leaves a SHA without "
                "a verdict."
            ),
            offence=judge(_pull_request_group_problems),
            red=mutants(
                CI,
                an_unconditional_ref_group=_set_concurrency(group=REF_ONLY_GROUP),
                a_group_keyed_on_the_commit_alone=_set_concurrency(
                    group="${{ github.workflow }}-${{ github.sha }}"
                ),
                cancel_in_progress_on_every_event=_set_concurrency(
                    **{"cancel-in-progress": True}
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-sweep-size-comments-match-the-job-count",
            reason=(
                "The comment above `concurrency` quotes the size of a full sweep, and it "
                "drifted to 17-18 against 25 (#8143). Both comments cite the job count a push "
                "and a pull request run alike, with each matrix counted by its include list "
                "or axis product and each event-gated job left out on the event that skips "
                "it. A matrix this count cannot read fails the row rather than passing it."
            ),
            offence=_sweep_citation_problems,
            green=(
                Subject(CI),
                _bare_sweep(
                    "# a full sweep\n# runs 5 jobs\n# the full sweep runs 5 jobs",
                    "  a:\n    strategy:\n      matrix:\n        shard: [1, 2, 3]\n"
                    "  b:\n    steps: []\n"
                    "  c:\n    if: github.event_name != 'push'\n    steps: []\n"
                    "  d:\n    if: github.event_name == 'push'\n    steps: []\n",
                ),
            ),
            red=(
                _bare_sweep(
                    "# a full sweep runs 17 jobs\n# a full sweep runs 2 jobs", _TWO_JOBS
                ),
                _bare_sweep("# a full sweep runs 2 jobs", _TWO_JOBS),
                _bare_sweep(
                    _SWEEP_OF_TWO,
                    _TWO_JOBS + "  c:\n    if: github.event_name != 'push'\n",
                ),
                _bare_sweep(
                    _SWEEP_OF_TWO,
                    "  a:\n    strategy:\n      matrix:\n        shard: [1]\n"
                    "        include: [{x: 1}]\n  b:\n    steps: []\n",
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-pytest-job-restores-rewritten-modules-before-the-tests",
            reason=(
                "Every xdist worker collects its whole shard, and on a cold checkout each one "
                "rewrites every test module's assertions itself (#8693). pytest validates a "
                "rewritten `.pyc` by the source's hash, so a cached one stays valid across "
                "checkouts. The cache helps only while its glob matches the name pytest "
                "writes, a name that carries pytest's version, so the glob is checked against "
                "`_pytest.assertion.rewrite.PYC_TAIL` rather than a literal. The ordinary "
                "bytecode glob does not reach pytest's files."
            ),
            offence=judge(_restore_before_tests_problems),
            red=mutants(
                CI,
                a_glob_missing_the_pytest_tag=_edit_restore(path=STALE_PYC_GLOB),
                a_restore_after_the_tests=_restore_after_tests,
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-rewrite-cache-restores-the-nightly-sweeps-entry",
            reason=(
                "No leg saves the rewrite cache: the nightly sweep in pytest-sweep.yml saves "
                "one entry over every module on master, which every leg of every event can "
                "read, whatever its shard packed. A leg's save, through `actions/cache/save` or "
                "the combined `actions/cache`, would hold its shard alone and become the newest "
                "match for every leg's `restore-keys`. The restore reads the path the sweep saves, "
                "byte for byte, since the cache version hashes the path, and its `restore-keys` "
                "is a prefix of the sweep's key, so it takes the newest sweep entry. A shard or "
                "a lock hash in the prefix would match no sweep entry."
            ),
            offence=judge(_rewrite_cache_problems),
            red=mutants(
                CI,
                restore_keys_that_name_the_shard=_edit_restore(
                    **{
                        "restore-keys": "pytest-rewrite-${{ runner.os }}-shard${{ matrix.shard }}-"
                    }
                ),
                restore_keys_that_hash_the_lock=_edit_restore(
                    **{
                        "restore-keys": "pytest-rewrite-${{ runner.os }}-${{ hashFiles('uv.lock') }}-"
                    }
                ),
                a_restore_with_no_restore_keys=_drop_restore_keys,
                a_restore_of_another_path=_edit_restore(path="**/__pycache__/*.pyc"),
                a_leg_that_saves_the_cache=_add_leg_save(SAVE),
                a_leg_that_also_caches_through_actions_cache=_add_leg_save(CACHE),
            ),
        )
    ),
)
