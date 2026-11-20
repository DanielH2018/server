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
    condition,
    judge,
    jobs,
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
DAY = "${{ steps.rewrite-day.outputs.day }}"
SHARD = "${{ matrix.shard }}"
MASTER_ONLY = "github.ref == 'refs/heads/master' && "
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


def _rewrite_step(wf: Workflow, action: str) -> dict | None:
    """The first `action` step of the pytest job whose path glob matches a rewritten module."""
    rewritten = PurePosixPath("tests/repo/__pycache__/test_x" + PYC_TAIL)
    for step in steps(ci_job(wf, PYTEST_JOB)):
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
    step = _rewrite_step(wf, RESTORE)
    if step is None:
        return [f"no {RESTORE} step matches the `*{PYC_TAIL}` files pytest writes"]
    if _after_tests(wf, step) is not False:
        return [f"the restore step does not run before {TEST_STEP!r}"]
    return []


def _restore_key_problems(wf: Workflow) -> list[str]:
    step = _rewrite_step(wf, RESTORE)
    if step is None:
        return [f"no {RESTORE} step matches the `*{PYC_TAIL}` files pytest writes"]
    key, restore = str(step["with"].get("key")), step["with"].get("restore-keys")
    found = []
    if SHARD not in key:
        found.append(f"the key {key!r} does not name the shard")
    if not key.endswith(f"-{DAY}"):
        found.append(f"the key {key!r} does not end in the day")
    if restore != key.removesuffix(DAY):
        found.append(f"restore-keys {restore!r} is not the key without the day")
    return found


def _save_problems(wf: Workflow) -> list[str]:
    restore, save = _rewrite_step(wf, RESTORE), _rewrite_step(wf, SAVE)
    if restore is None or save is None:
        return ["the pytest job lacks a rewrite-cache restore step or save step"]
    found = []
    if save["with"].get("path") != restore["with"].get("path"):
        found.append("the save path differs from the restore path")
    primary = f"${{{{ steps.{restore.get('id')}.outputs.cache-primary-key }}}}"
    if save["with"].get("key") != primary:
        found.append(f"the save key is not {primary}")
    gate = condition(save)
    if "github.ref == 'refs/heads/master'" not in gate:
        found.append(f"the save gate {gate!r} does not name master")
    if f"steps.{restore.get('id')}.outputs.cache-hit != 'true'" not in gate:
        found.append(f"the save gate {gate!r} saves a key the restore hit")
    if _after_tests(wf, save) is not True:
        found.append(f"the save step does not run after {TEST_STEP!r}")
    return found


def _edit_step(action: str, **fields: object) -> Callable[[Workflow], None]:
    """A mutation that sets keys of `with` on the pytest job's rewrite step for `action`."""

    def mutate(wf: Workflow) -> None:
        step = _rewrite_step(wf, action)
        assert step is not None
        step["with"].update(fields)

    return mutate


def _edit_restore_key(change: Callable[[str], str]) -> Callable[[Workflow], None]:
    """A mutation that rewrites the restore step's cache key with `change`."""

    def mutate(wf: Workflow) -> None:
        step = _rewrite_step(wf, RESTORE)
        assert step is not None
        step["with"]["key"] = change(step["with"]["key"])

    return mutate


def _edit_save_gate(find: str) -> Callable[[Workflow], None]:
    def mutate(wf: Workflow) -> None:
        save = _rewrite_step(wf, SAVE)
        assert save is not None
        save["if"] = condition(save).replace(find, "")

    return mutate


def _move(action: str, before_tests: bool) -> Callable[[Workflow], None]:
    """A mutation that moves the rewrite step for `action` to either side of the test step."""

    def mutate(wf: Workflow) -> None:
        job = wf["jobs"][PYTEST_JOB]
        moved = _rewrite_step(wf, action)
        rest = [s for s in steps(job) if s is not moved]
        at = [s.get("name") for s in rest].index(TEST_STEP) + (not before_tests)
        job["steps"] = [*rest[:at], moved, *rest[at:]]

    return mutate


def _drop_save(wf: Workflow) -> None:
    job = wf["jobs"][PYTEST_JOB]
    save = _rewrite_step(wf, SAVE)
    job["steps"] = [s for s in steps(job) if s is not save]


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
                a_glob_missing_the_pytest_tag=_edit_step(RESTORE, path=STALE_PYC_GLOB),
                a_restore_after_the_tests=_move(RESTORE, before_tests=False),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-rewrite-cache-restores-the-newest-entry-and-names-one-per-day",
            reason=(
                "Each shard restores its newest entry and writes one entry per day (#8693). "
                "The key names the shard and ends in the day. `restore-keys` is the key "
                "without the day, so a restore falls back to the newest earlier entry of the "
                "same shard."
            ),
            offence=judge(_restore_key_problems),
            red=mutants(
                CI,
                a_key_without_the_shard=_edit_restore_key(
                    lambda key: key.replace(SHARD, "1")
                ),
                a_key_without_the_day=_edit_restore_key(
                    lambda key: key.removesuffix(DAY)
                ),
                restore_keys_that_name_no_shard=_edit_step(
                    RESTORE, **{"restore-keys": "pytest-rewrite-"}
                ),
            ),
        )
    ),
    Census(
        **ci_row(
            name="ci-only-master-saves-the-rewrite-cache-once-per-key",
            reason=(
                "Only master saves the rewritten modules, once per shard per day, and only a "
                "key it did not restore. A per-run or per-branch save would churn a cache "
                "budget already over 10 GB and evict the pinned-tool caches the workflow "
                "reads (#8693). The save writes the path the restore reads, under the key "
                "the restore computed, after the tests have written the files."
            ),
            offence=judge(_save_problems),
            red=mutants(
                CI,
                a_save_on_every_branch=_edit_save_gate(MASTER_ONLY),
                a_save_over_a_key_the_restore_hit=_edit_save_gate(
                    " && steps.rewrite-cache.outputs.cache-hit != 'true'"
                ),
                a_save_under_a_per_run_key=_edit_step(
                    SAVE, key="pytest-rewrite-${{ github.run_id }}"
                ),
                a_save_of_another_path=_edit_step(SAVE, path="**/__pycache__/*.pyc"),
                a_save_before_the_tests=_move(SAVE, before_tests=True),
                a_pytest_job_with_no_save=_drop_save,
            ),
        )
    ),
)
