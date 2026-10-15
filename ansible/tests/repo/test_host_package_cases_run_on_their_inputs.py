"""A PR that changes only the container cases' inputs runs those cases, on both legs (#5004).

`host_packages` in ci.yml runs its container cases (H3.3b) when step 2's render diff reports
`changed=true`, or on a dispatch. The cases' own inputs are not render inputs: the test image's
Dockerfile, the fixture specs under `hosts/lib/tests/container/fixture/`, and the scripts that
drive the cases. A PR touching only those changes no render, so its run skipped the cases and
merged green without them.

HOW THE JOB IS SIMULATED. Each test builds a PR merge ref in a scratch clone of this repository
that commits one edit to one input. It then walks the job's steps in order, up to the cases step:

- A step with both an `id:` and a `run:` whose `if:` holds is RUN, as the runner would run it.
  The clone is its working directory and its `$GITHUB_WORKSPACE`. Its own `env:` block is
  resolved against the simulated contexts, and what it writes to `$GITHUB_OUTPUT` becomes
  `steps.<id>.outputs`. `uv` runs against this test's own environment with syncing off.
- The `render` step computes `changed` (the render diff) and `cases` (the trigger under test),
  so it runs in full except for its two slow calls, `host_packages.py render` and `diff`. A
  `uv` shim on its PATH answers those: `render` creates its `--out` directory and `diff` prints
  `changed=false`. Every other `uv` call, `job-changed` and `fixture-changed` included, reaches
  the real `uv`, unless the test names one in `crash`; the shim then fails it as a traceback
  would. The simulation fails unless the step makes exactly two `render` calls and one `diff`
  call, so a renamed subcommand cannot turn the stub into a silent no-op.
- Every other step is not run: the nfpm fetch, the builds and the cases themselves.

The render is stubbed as unchanged because no input this module edits is a render input, so the
real diff reports `changed=false` for each. That is the worst case for the trigger: the cases
can run only because `cases=true`. A real render per input and leg took 80s on the CI runner,
alone in one pytest shard. `test_the_real_render_reports_no_change_for_a_case_input` runs the
real render for the Dockerfile on one leg, so the stub cannot drift from the renderer unseen.

The test then evaluates the `if:` of every step the cases depend on, once per matrix leg.
`uses:` steps produce no outputs here, so a trigger computed by an action is invisible to the
simulation. The evaluator raises on an expression form it does not implement rather than
reading it as false.

Run: uv run pytest ansible/tests/repo/test_host_package_cases_run_on_their_inputs.py
"""

import re
import shutil
import sys
from pathlib import Path

import pytest
from _actions_expressions import condition, interpolate, read_outputs
from _helpers import REPO
from lib import yaml_fast
from lib.git_testing import git, git_out, scrubbed_env
from lib.proc_testing import fake_bin, run

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
JOB = "host_packages"

RENDER_ID = "render"
CASES_STEP = "Run the updater cases in systemd containers"
# The steps the cases read from. `host_package_cases.py` globs each side's updater .deb out of
# the build step's output and boots the image the image step tags, so a run that skips either
# fails the cases rather than running them.
CASE_PREREQUISITES = (
    "Build both renders with the pinned nfpm",
    "Build the test image for this release",
)

DOCKERFILE = "hosts/lib/tests/container/Dockerfile"
# The inputs #5004 names, each by a concrete member. The nested fixture file catches a trigger
# that matches exact names where it should match the directory.
NAMED_INPUTS = (
    "hosts/lib/tests/container/fixture/base.yml",
    "hosts/lib/tests/container/fixture/units/homelab-fixture-kept.service",
    "scripts/validate/host_package_cases.py",
    "scripts/validate/validate_lib/host_fixtures.py",
    "scripts/validate/validate_lib/host_container.py",
)
UNRELATED = "README.md"


def _tracked_container_inputs() -> list[str]:
    """Every tracked file under the image's build context, which is the census half."""
    out = git_out(REPO, "ls-files", "hosts/lib/tests/container")
    files = out.splitlines()
    assert DOCKERFILE in files, f"git ls-files lost {DOCKERFILE}: {files}"
    return files


def _case_inputs() -> list[str]:
    paths = dict.fromkeys([*NAMED_INPUTS, *_tracked_container_inputs()])
    paths.pop(DOCKERFILE, None)
    return list(paths)


@pytest.fixture(scope="module")
def clone(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone of this repository's HEAD, sharing its object store.

    `--shared` reads objects through an alternate, so HEAD resolves even when it is a detached
    merge ref that no branch carries, as it is in CI's pytest job.

    `origin` is the clone itself, so `origin/master` is the PR's base, as on the runner, and
    `git fetch origin master` (what `scripts/dev/pr_changed_files.sh` runs) works offline. The
    prune drops the remote-tracking refs the clone copied from this checkout's branches."""
    repo = tmp_path_factory.mktemp("host-cases") / "clone"
    head = git_out(REPO, "rev-parse", "HEAD")
    git(REPO.parent, "clone", "-q", "--shared", "--no-checkout", str(REPO), str(repo))
    git(repo, "checkout", "-q", "--detach", head)
    git(repo, "branch", "-f", "master", head)
    git(repo, "remote", "set-url", "origin", str(repo))
    git(repo, "fetch", "-q", "--prune", "--no-tags", "origin")
    return repo


def _pr_merge_ref(clone: Path, path: str) -> None:
    """Stand where a pull_request run stands: master merged with a PR editing only `path`."""
    branch = "pr-" + re.sub(r"[^A-Za-z0-9]+", "-", path)
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "checkout", "-q", "-B", branch)
    target = clone / path
    target.write_text(target.read_text() + "\n# touched by the #5004 test\n")
    git(clone, "commit", "-q", "-am", f"edit {path}")
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "merge", "-q", "--no-ff", "--no-edit", branch)


# Answers `uv run python scripts/validate/host_packages.py render|diff`, and fails the subcommand
# named in $SHIM_CRASH; any other call goes on to uv.
_UV_SHIM = """#!/bin/bash
if [ "$1 $2 $3" = "run python scripts/validate/host_packages.py" ]; then
  if [ "$4" = "$SHIM_CRASH" ]; then
    echo "Traceback (most recent call last): $4 crashed in the #5071 test" >&2; exit 1
  fi
  case "$4" in
    render) echo render >> "$SHIM_CALLS"; mkdir -p "${@: -1}"; exit 0 ;;
    diff) echo diff >> "$SHIM_CALLS"; echo changed=false; exit 0 ;;
  esac
fi
exec "$REAL_UV" "$@"
"""


class StepFailed(AssertionError):
    """A simulated step exited non-zero.

    Attributes:
        outputs: what the step had written to `$GITHUB_OUTPUT` when it exited.
    """

    def __init__(self, message: str, outputs: dict[str, str]) -> None:
        super().__init__(message)
        self.outputs = outputs


def _simulate(
    clone: Path,
    tmp_path: Path,
    leg: dict,
    *,
    stub_render: bool = True,
    crash: str = "",
) -> tuple[dict[str, bool], dict]:
    """Run the job's output-producing steps on one leg; return each step's verdict and the context.

    `stub_render` answers the render step's two slow calls with the `uv` shim (see the docstring),
    and `crash` names a `host_packages.py` subcommand the shim fails.

    Raises:
        StepFailed: a step exited non-zero.
    """
    workflow = yaml_fast.safe_load(CI_WORKFLOW.read_text())
    job = workflow["jobs"][JOB]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(parents=True)
    context: dict = {
        "github": {
            "event_name": "pull_request",
            "base_ref": "master",
            "head_ref": "pr",
            "ref": "refs/pull/1/merge",
            "sha": git_out(clone, "rev-parse", "HEAD"),
            "workspace": str(clone),
            "token": "test-token",
            "event": {
                "number": 1,
                "pull_request": {
                    "number": 1,
                    "base": {
                        "ref": "master",
                        "sha": git_out(clone, "rev-parse", "HEAD^1"),
                    },
                    "head": {"sha": git_out(clone, "rev-parse", "HEAD^2")},
                },
            },
        },
        "matrix": leg,
        "steps": {},
        "env": {},
        "runner": {"temp": str(runner_temp), "os": "Linux"},
        "secrets": {},
        "vars": {},
        "inputs": {},
    }
    for scope in (workflow.get("env") or {}, job.get("env") or {}):
        context["env"].update({k: interpolate(v, context) for k, v in scope.items()})

    verdicts: dict[str, bool] = {}
    for index, step in enumerate(job["steps"]):
        name = step.get("name", step.get("uses", f"step {index}"))
        holds = condition(step, context)
        verdicts[name] = holds
        if name == CASES_STEP:
            context["cases_env"] = {
                k: interpolate(v, context) for k, v in (step.get("env") or {}).items()
            }
            return verdicts, context
        if not (holds and "id" in step and "run" in step):
            continue
        output = runner_temp / f"output-{index}"
        output.write_text("")
        stub = stub_render and step["id"] == RENDER_ID
        calls = runner_temp / "shim-calls"
        calls.write_text("")
        env = scrubbed_env(
            **context["env"],
            **{k: interpolate(v, context) for k, v in (step.get("env") or {}).items()},
            RUNNER_TEMP=str(runner_temp),
            GITHUB_OUTPUT=str(output),
            GITHUB_ENV=str(runner_temp / "env"),
            GITHUB_PATH=str(runner_temp / "path"),
            GITHUB_STEP_SUMMARY=str(runner_temp / "summary.md"),
            GITHUB_EVENT_NAME="pull_request",
            GITHUB_BASE_REF="master",
            GITHUB_WORKSPACE=str(clone),
            UV_PROJECT_ENVIRONMENT=sys.prefix,
            UV_NO_SYNC="1",
            REAL_UV=shutil.which("uv") or "",
            SHIM_CALLS=str(calls),
            SHIM_CRASH=crash,
        )
        script = interpolate(step["run"], context)
        proc = run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=clone,
            env=env,
            stub_bin=fake_bin(runner_temp / "shim", uv=_UV_SHIM) if stub else None,
            timeout=300,
        )
        if proc.returncode != 0:
            raise StepFailed(
                f"step {name!r} failed on the simulated PR:\n{proc.stdout}\n{proc.stderr}",
                read_outputs(output.read_text()),
            )
        if stub:
            assert calls.read_text().split() == ["render", "render", "diff"], (
                "the render step no longer makes the two render calls and one diff call stubbed"
            )
        context["steps"][step["id"]] = {
            "outputs": read_outputs(output.read_text()),
            "outcome": "success",
            "conclusion": "success",
        }
    raise AssertionError(f"job {JOB} has no step named {CASES_STEP!r}")


def _legs() -> list[dict]:
    job = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"][JOB]
    legs = job["strategy"]["matrix"]["include"]
    assert len(legs) >= 2, f"{JOB} lost a matrix leg: {legs}"
    return legs


def _runs_per_leg(
    clone: Path, tmp_path: Path, path: str
) -> list[tuple[dict, dict[str, bool], dict]]:
    _pr_merge_ref(clone, path)
    results = []
    for number, leg in enumerate(_legs()):
        verdicts, context = _simulate(clone, tmp_path / f"leg-{number}", leg)
        results.append((leg, verdicts, context))
    return results


def _assert_cases_run(clone: Path, tmp_path: Path, path: str) -> None:
    for leg, verdicts, context in _runs_per_leg(clone, tmp_path, path):
        render = context["steps"].get("render", {}).get("outputs", {})
        assert verdicts[CASES_STEP], (
            f"a PR touching only {path} skips {CASES_STEP!r} on leg {leg} "
            f"(render outputs {render}), so it merges green without the cases it changed"
        )
        assert context["cases_env"].get("UPGRADE") == "true", (
            f"the cases step runs on leg {leg} but stands down: UPGRADE="
            f"{context['cases_env'].get('UPGRADE')!r}"
        )


def test_a_dockerfile_only_pr_runs_the_cases_on_both_legs(
    clone: Path, tmp_path: Path
) -> None:
    """#5004's Verify-by. The README edit after it is the control: the trigger is the inputs,
    not every PR."""
    _assert_cases_run(clone, tmp_path / "dockerfile", DOCKERFILE)

    for leg, verdicts, _context in _runs_per_leg(
        clone, tmp_path / "control", UNRELATED
    ):
        assert not verdicts[CASES_STEP], (
            f"a PR touching only {UNRELATED} runs the cases on leg {leg}; the trigger has to "
            f"be the cases' inputs, not every pull request"
        )


def test_a_dockerfile_only_pr_builds_the_debs_and_image_the_cases_read(
    clone: Path, tmp_path: Path
) -> None:
    for leg, verdicts, _context in _runs_per_leg(clone, tmp_path, DOCKERFILE):
        skipped = [name for name in CASE_PREREQUISITES if not verdicts.get(name)]
        assert not skipped, (
            f"a PR touching only {DOCKERFILE} skips {skipped} on leg {leg}; the cases read "
            f"the debs and the image those steps produce"
        )


def test_the_real_render_reports_no_change_for_a_case_input(
    clone: Path, tmp_path: Path
) -> None:
    """The stub's premise, and the only real render: a case input is not a render input."""
    _pr_merge_ref(clone, DOCKERFILE)
    _verdicts, context = _simulate(clone, tmp_path, _legs()[0], stub_render=False)
    outputs = context["steps"][RENDER_ID]["outputs"]
    assert outputs.get("changed") == "false", (
        f"a PR touching only {DOCKERFILE} changes a render ({outputs}); the other tests stub "
        f"the render as unchanged, so they no longer model that PR"
    )
    assert outputs.get("cases") == "true", outputs


@pytest.mark.parametrize("path", _case_inputs())
def test_a_pr_touching_only_a_case_input_runs_the_cases(
    clone: Path, tmp_path: Path, path: str
) -> None:
    _assert_cases_run(clone, tmp_path, path)


CASES_INVOCATION = '--work "$RUNNER_TEMP/cases"'


def test_a_pr_editing_only_the_cases_step_runs_the_cases(
    clone: Path, tmp_path: Path
) -> None:
    """The job's own steps invoke the cases, so an edit to one is an input of the cases too.

    The edit changes the cases step's command line, which no render reads. The README control
    in `test_a_dockerfile_only_pr_runs_the_cases_on_both_legs` covers the edit that must not
    trigger them.
    """
    workflow = ".github/workflows/ci.yml"
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "checkout", "-q", "-B", "pr-cases-step")
    target = clone / workflow
    text = target.read_text()
    assert text.count(CASES_INVOCATION) == 1, f"{workflow} lost {CASES_INVOCATION!r}"
    target.write_text(
        text.replace(CASES_INVOCATION, '--work "$RUNNER_TEMP/cases-edited"')
    )
    git(clone, "commit", "-q", "-am", "edit the cases step")
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "merge", "-q", "--no-ff", "--no-edit", "pr-cases-step")
    for number, leg in enumerate(_legs()):
        verdicts, context = _simulate(clone, tmp_path / f"leg-{number}", leg)
        assert verdicts[CASES_STEP], (
            f"a PR editing only the cases step skips it on leg {leg} "
            f"(render outputs {context['steps']['render']['outputs']})"
        )


@pytest.mark.parametrize("subcommand", ["job-changed", "fixture-changed"])
def test_a_crashing_verdict_fails_the_render_step(
    clone: Path, tmp_path: Path, subcommand: str
) -> None:
    """#5071's Verify-by: a verdict that cannot be computed fails the step, never reads false.

    The README PR fires no other trigger, so a step that swallowed the crash would write
    `cases=false` and succeed.
    """
    _pr_merge_ref(clone, UNRELATED)
    with pytest.raises(StepFailed, match="Render every host package") as failed:
        _simulate(clone, tmp_path, _legs()[0], crash=subcommand)
    assert "cases" not in failed.value.outputs, (
        f"the render step wrote cases={failed.value.outputs['cases']} although "
        f"{subcommand} crashed"
    )


RENDERER = "scripts/renderer/renderer_lib/host.py"
RENDERER_ANCHOR = "        package.files[dst] = (content, mode)\n"


def test_a_renderer_change_seen_only_in_the_fixture_runs_the_cases(
    clone: Path, tmp_path: Path
) -> None:
    """#5066's Verify-by: the renderer changes only how it renders a systemd drop-in.

    No real package ships a drop-in, so the stubbed `changed=false` is what the real diff
    reports; only the fixture's render differs. The comment-only edit after it is the control:
    the trigger is the fixture's render, not every edit to the renderer's files.
    """
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "checkout", "-q", "-B", "pr-renderer-dropin")
    target = clone / RENDERER
    text = target.read_text()
    assert text.count(RENDERER_ANCHOR) == 1, f"{RENDERER} lost {RENDERER_ANCHOR!r}"
    edited = (
        "        if PurePosixPath(dst).name == DROPIN:\n"
        '            content += b"# rendered differently by the #5066 test\\n"\n'
    )
    target.write_text(text.replace(RENDERER_ANCHOR, edited + RENDERER_ANCHOR))
    git(clone, "commit", "-q", "-am", "render a drop-in differently")
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "merge", "-q", "--no-ff", "--no-edit", "pr-renderer-dropin")
    for number, leg in enumerate(_legs()):
        verdicts, context = _simulate(clone, tmp_path / f"leg-{number}", leg)
        assert verdicts[CASES_STEP], (
            f"a PR changing only how {RENDERER} renders a drop-in skips the cases on leg "
            f"{leg} (render outputs {context['steps']['render']['outputs']})"
        )
        assert context["cases_env"].get("UPGRADE") == "true", context["cases_env"]

    for leg, verdicts, _context in _runs_per_leg(clone, tmp_path / "control", RENDERER):
        assert not verdicts[CASES_STEP], (
            f"a comment-only edit to {RENDERER} runs the cases on leg {leg}; the trigger has "
            f"to be the fixture's render, not the renderer's files"
        )
